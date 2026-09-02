from ai_runtime.dependencies import *
from ai_runtime.config import _is_gpt5_model, _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection


def _responses_reasoning_payload(
    reasoning_selection: ReasoningSelection | dict | str | None,
) -> dict | None:
    """Translate an already validated neutral choice for the Responses API."""

    if reasoning_selection is None:
        return None
    selection = parse_reasoning_selection(reasoning_selection)
    if selection.mode == "default":
        return None
    # ``none`` is the Responses spelling for the neutral ``off`` control.
    effort = "none" if selection.mode == "off" else selection.mode
    return {"effort": effort, "summary": "auto"}

def _convert_messages_for_responses_api(messages: list) -> list:
    """Convert Chat Completions message content blocks to Responses API format.

    Chat Completions uses: type: "text", type: "image_url"
    Responses API uses:    type: "input_text", type: "input_image", type: "output_text"

    String content and non-dict items (e.g. Responses API function_call items) pass through unchanged.
    """
    converted = []
    for msg in messages:
        if not isinstance(msg, dict):
            converted.append(msg)
            continue

        # Responses API native items (function_call, function_call_output) pass through
        if "type" in msg and "role" not in msg:
            converted.append(msg)
            continue

        role = msg.get("role", "user")
        content = msg.get("content")

        # String content or None: works as-is in Responses API
        if content is None or isinstance(content, str):
            converted.append(msg)
            continue

        if isinstance(content, list):
            new_content = []
            for block in content:
                if not isinstance(block, dict):
                    new_content.append(block)
                    continue
                btype = block.get("type")
                if btype == "text":
                    new_block = {
                        "type": "output_text" if role == "assistant" else "input_text",
                        "text": block.get("text", ""),
                    }
                    new_content.append(new_block)
                elif btype == "image_url":
                    if role == "assistant":
                        # Assistant messages only support output_text/refusal in Responses API.
                        # Replace with placeholder so the AI knows it generated an image
                        # (prevents confusion / hallucinated URLs in follow-up turns).
                        new_content.append({
                            "type": "output_text",
                            "text": "[An image was generated and displayed to the user]",
                        })
                        continue
                    img_data = block.get("image_url", {})
                    url = img_data.get("url", "") if isinstance(img_data, dict) else str(img_data)
                    new_content.append({"type": "input_image", "image_url": url})
                elif btype == "document_url":
                    fn = block.get("document_url", {}).get("filename", "document.pdf")
                    new_content.append({
                        "type": "input_text",
                        "text": f"[PDF document: {fn} -- content unavailable in this format]"
                    })
                elif btype == "file":
                    file_info = block.get("file", {}) if isinstance(block.get("file"), dict) else {}
                    file_data = file_info.get("file_data")
                    if file_data:
                        new_content.append({
                            "type": "input_file",
                            "filename": file_info.get("filename") or "document.pdf",
                            "file_data": file_data,
                        })
                    else:
                        new_content.append({
                            "type": "input_text",
                            "text": f"[File attachment: {file_info.get('filename') or 'document.pdf'} -- content unavailable]"
                        })
                elif btype == "text_file":
                    new_content.append({
                        "type": "input_text",
                        "text": text_file_block_to_text(block)
                    })
                else:
                    # Unknown block type, pass through
                    new_content.append(block)
            converted.append({**msg, "content": new_content})
        else:
            converted.append(msg)

    return converted


async def call_gpt_responses_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                                  input_token_fallback=None,
                                  pdf_error_metadata=None,
                                  prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                                  llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                                  pending_attachment_refs: Optional[list[str]] = None,
                                  perf_trace=None,
                                  strip_device_action_blocks: bool = False,
                                  billing_reservation_id: str | None = None,
                                  reasoning_selection: ReasoningSelection | dict | str | None = None):
    """
    OpenAI Responses API call function. Replaces call_gpt_api for all OpenAI calls.
    Uses /v1/responses endpoint with semantic SSE events instead of Chat Completions.

    Emits the same SSE format as call_llm_api() for frontend compatibility:
    - data: {"content": "chunk"}
    - data: {"tool_call": {...}, "tool_call_pending": true}
    - data: {"searching": true/false}
    - data: {"web_search_citations": {...}}
    - data: {"message_ids": {...}}
    - data: {"token_info": true, ...}
    - data: {"error": "..."}
    - data: [DONE]
    """
    global stop_signals
    logger.info("enters call_gpt_responses_api")

    error_yielded = False
    api_url = "https://api.openai.com/v1/responses"
    api_key = user_api_key or openai_key

    user_id = current_user.id

    # Convert Chat Completions message format to Responses API format
    # (type: "text" -> "input_text"/"output_text", type: "image_url" -> "input_image")
    messages = _convert_messages_for_responses_api(messages)

    # Build request body (Responses API format)
    data = {
        "model": model,
        "input": messages,
        "stream": True,
        "store": False,
    }
    reasoning = _responses_reasoning_payload(reasoning_selection)
    if reasoning is not None:
        data["reasoning"] = reasoning

    # System prompt goes in 'instructions' (top-level, not in input array)
    if prompt:
        data["instructions"] = prompt

    # GPT-5+ models don't support custom temperature
    if not _is_gpt5_model(model):
        data["temperature"] = temperature

    # Responses API uses max_output_tokens
    if max_tokens:
        data["max_output_tokens"] = max_tokens

    # Shallow copy to avoid mutating the caller's list if server tools are appended later
    if tools:
        data["tools"] = list(tools)
        data["tool_choice"] = "auto"
        # Needed verbatim in a later function-call continuation for reasoning models.
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

    logger.info(f"call_gpt_responses_api -> model: {model}, tools: {len(tools) if tools else 0}")
    if perf_trace:
        event = perf_trace.sse(
            "openai_request_start",
            model=model,
            input_items=len(messages or []),
            instructions_chars=len(prompt or ""),
            max_output_tokens=max_tokens,
            tool_count=len(tools or []),
        )
        if event:
            yield event

    # GPT-5+ are reasoning models and may need more time
    timeout_seconds = 300 if _is_gpt5_model(model) else 120
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(api_url, headers=headers, json=data) as response:
                if perf_trace:
                    event = perf_trace.sse("openai_headers_received", status=response.status)
                    if event:
                        yield event
                if response.status == 200:
                    buffer = ""
                    raw_remainder = b""  # Holds incomplete UTF-8 bytes across chunks
                    first_event_marked = False
                    first_text_marked = False

                    async for chunk in response.content.iter_any():
                        if stop_signals.get(conversation_id):
                            logger.info("Stop signal received, exiting Responses API call loop.")
                            break

                        # Buffer raw bytes to handle multi-byte UTF-8 chars split across reads
                        raw_remainder += chunk
                        try:
                            chunk_str = raw_remainder.decode("utf-8")
                            raw_remainder = b""
                        except UnicodeDecodeError:
                            # Incomplete multi-byte char at the end — keep buffering
                            # Try to decode all but the last 1-3 bytes
                            for trim in range(1, 4):
                                try:
                                    chunk_str = raw_remainder[:-trim].decode("utf-8")
                                    raw_remainder = raw_remainder[-trim:]
                                    break
                                except UnicodeDecodeError:
                                    continue
                            else:
                                continue  # Can't decode anything yet, wait for more data

                        buffer += chunk_str

                        # Process complete SSE events (separated by double newline)
                        while "\n\n" in buffer:
                            event_block, buffer = buffer.split("\n\n", 1)

                            # Parse event type and data from SSE block
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

                            if not first_event_marked and perf_trace:
                                event = perf_trace.sse("openai_first_event")
                                if event:
                                    yield event
                                first_event_marked = True

                            try:
                                event_data = orjson.loads(data_str)
                            except orjson.JSONDecodeError as e:
                                logger.warning(f"[call_gpt_responses_api] JSON decode warning: {e}")
                                continue

                            # Use event_type from SSE 'event:' line (more reliable)
                            # Fall back to data.type if event line is missing
                            etype = event_type or event_data.get("type", "")

                            # --- Text streaming ---
                            if etype == "response.output_text.delta":
                                delta = event_data.get("delta", "")
                                if delta:
                                    if thinking_open:
                                        thinking_open = False
                                        yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"
                                    if not first_text_marked and perf_trace:
                                        event = perf_trace.sse("openai_first_text_delta")
                                        if event:
                                            yield event
                                        first_text_marked = True
                                    content += delta
                                    yield f"data: {orjson.dumps({'content': delta}).decode()}\n\n"

                            # OpenAI exposes a compact reasoning summary separately from
                            # answer text.  Keep it in the product's existing SSE contract.
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
                                pass  # Intermediate search status, no action needed

                            elif etype == "response.web_search_call.completed":
                                yield f"data: {orjson.dumps({'searching': False}).decode()}\n\n"

                            # --- Function call handling ---
                            elif etype == "response.output_item.added":
                                item = event_data.get("item", {})
                                if isinstance(item, dict):
                                    response_output_items.append(item)
                                if item.get("type") == "function_call":
                                    function_name = item.get("name", "")
                                    tool_call_id = item.get("call_id", "")
                                    function_arguments = ""
                                    function_output_item = item

                            elif etype == "response.function_call_arguments.delta":
                                function_arguments += event_data.get("delta", "")
                                if function_output_item is not None:
                                    function_output_item["arguments"] = function_arguments

                            elif etype == "response.function_call_arguments.done":
                                # Function call arguments are complete
                                # Will be processed after the stream loop
                                pass

                            # --- Citations ---
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
                                    # Added events may contain partial stubs.  The completed
                                    # response is authoritative and includes encrypted state.
                                    response_output_items = final_output
                                incomplete_reason = (resp.get("incomplete_details") or {}).get("reason")
                                if not truncated and (resp.get("status") == "incomplete" or incomplete_reason in {"max_output_tokens", "max_tokens"}):
                                    truncated = True
                                    _log_truncated_response(
                                        "OpenAI Responses",
                                        model,
                                        conversation_id,
                                        llm_id,
                                        incomplete_reason or resp.get("status") or "incomplete",
                                        max_tokens,
                                    )

                                # Extract usage
                                usage = resp.get("usage", {})
                                input_tokens = usage.get("input_tokens", 0)
                                output_tokens = usage.get("output_tokens", 0)
                                total_tokens = input_tokens + output_tokens
                                if perf_trace:
                                    event = perf_trace.sse(
                                        "openai_completed_event",
                                        input_tokens=input_tokens,
                                        output_tokens=output_tokens,
                                        total_tokens=total_tokens,
                                    )
                                    if event:
                                        yield event

                                # Extract any remaining citations from completed response
                                for output_item in resp.get("output", []):
                                    if output_item.get("type") == "message":
                                        for part in output_item.get("content", []):
                                            for ann in part.get("annotations", []):
                                                if ann.get("type") == "url_citation":
                                                    url = ann.get("url", "")
                                                    # Avoid duplicates
                                                    if not any(c["url"] == url and c.get("start_index") == ann.get("start_index") for c in citations):
                                                        citations.append({
                                                            "url": url,
                                                            "title": ann.get("title", ""),
                                                            "start_index": ann.get("start_index"),
                                                            "end_index": ann.get("end_index"),
                                                        })

                            # --- Errors ---
                            elif etype == "response.failed":
                                error_info = event_data.get("response", {}).get("error", {})
                                error_msg = error_info.get("message", "Unknown API error")
                                error_code = error_info.get("code") or error_info.get("type")
                                if isinstance(error_code, str) and error_code.strip() and error_code.strip() not in error_msg:
                                    error_msg = f"{error_code.strip()}: {error_msg}"
                                logger.error(f"[call_gpt_responses_api] Response failed: {error_msg}")
                                await record_provider_error_for_label(
                                    "OpenAI (GPT)",
                                    message=error_msg,
                                    model=model,
                                    byok=byok,
                                )
                                yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', error_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                                error_yielded = True

                            elif etype == "response.incomplete":
                                resp = event_data.get("response", {})
                                reason = (resp.get("incomplete_details") or {}).get("reason") or "incomplete"
                                if not truncated:
                                    truncated = True
                                    _log_truncated_response("OpenAI Responses", model, conversation_id, llm_id, reason, max_tokens)

                            # --- Refusal ---
                            elif etype == "response.refusal.delta":
                                delta = event_data.get("delta", "")
                                if delta:
                                    content += delta
                                    yield f"data: {orjson.dumps({'content': delta}).decode()}\n\n"

                else:
                    error_body = await response.text()
                    raw_log = f"[call_gpt_responses_api] Error: status {response.status}. Body: {error_body}"
                    logger.error(raw_log)
                    human_msg = _extract_human_error_message(error_body, response.status, "OpenAI (GPT)")
                    await record_provider_error_for_label(
                        "OpenAI (GPT)",
                        message=human_msg,
                        status_code=response.status,
                        model=model,
                        byok=byok,
                    )
                    yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
                    error_yielded = True

        except asyncio.TimeoutError as exc:
            error_message = f"[call_gpt_responses_api] Request timed out after {timeout_seconds}s for model {model}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "OpenAI (GPT)")
            await record_provider_error_for_label("OpenAI (GPT)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except aiohttp.ClientError as exc:
            error_message = f"[call_gpt_responses_api] Network error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "OpenAI (GPT)")
            await record_provider_error_for_label("OpenAI (GPT)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except Exception as exc:
            error_message = f"[call_gpt_responses_api] Unexpected error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "OpenAI (GPT)")
            await record_provider_error_for_label("OpenAI (GPT)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

    if thinking_open:
        yield f"data: {orjson.dumps({'type': 'thinking_end'}).decode()}\n\n"

    # Emit citations if any were collected (native web search)
    if citations:
        yield f"data: {orjson.dumps({'type': 'web_search_citations', 'citations': citations}).decode()}\n\n"

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
                input_payload=(prompt, messages),
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
    if function_name and save_to_db:
        try:
            parsed_args = orjson.loads(function_arguments) if function_arguments else {}
        except orjson.JSONDecodeError:
            logger.error(f"[call_gpt_responses_api] Failed to parse tool arguments: {function_arguments}")
            parsed_args = {}

        logger.info(f"[call_gpt_responses_api] Tool call detected: {function_name}")
        logger.debug(f"[call_gpt_responses_api] Tool call args: {parsed_args}")

        await record_provider_success_for_label("OpenAI (GPT)", model=model, byok=byok)
        yield f"data: {orjson.dumps({'tool_call': {'name': function_name, 'arguments': parsed_args, 'id': tool_call_id, 'response_output_items': response_output_items, '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}}).decode()}\n\n"
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
                               f"Provider: gpt_responses. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label("OpenAI (GPT)", message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            await record_provider_success_for_label("OpenAI (GPT)", model=model, byok=byok)
            citations_data = orjson.dumps(citations).decode() if citations else None
            if perf_trace:
                event = perf_trace.sse("db_save_start")
                if event:
                    yield event
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, current_user.id, model, user_message=user_message,
                                                                        input_token_fallback=input_token_fallback,
                                                                        prompt_id=prompt_id, watchdog_config=watchdog_config, watchdog_hint_active=watchdog_hint_active, watchdog_hint_eval_id=watchdog_hint_eval_id,
                                                                        llm_id=llm_id, citations_json=citations_data, byok=byok, pending_attachment_refs=pending_attachment_refs,
                                                                        strip_device_action_blocks=strip_device_action_blocks,
                                                                        billing_reservation_id=billing_reservation_id,
                                                                        billing_only_accumulated_usage=bool(billing_reservation_id))
            if perf_trace:
                event = perf_trace.sse(
                    "db_save_done",
                    user_message_id=user_message_id,
                    bot_message_id=bot_message_id,
                )
                if event:
                    yield event
            if user_message_id and bot_message_id:
                yield f"data: {orjson.dumps({'message_ids': {'user': user_message_id, 'bot': bot_message_id}}).decode()}\n\n"
            else:
                yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
                return

        if perf_trace:
            event = perf_trace.sse("openai_stream_done")
            if event:
                yield event
        yield content.strip()
    else:
        if content.strip():
            await record_provider_success_for_label("OpenAI (GPT)", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("OpenAI (GPT)", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('OpenAI (GPT)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"
