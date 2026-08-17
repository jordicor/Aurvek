from ai_runtime.dependencies import *
from ai_runtime.config import _log_truncated_response
from ai_runtime.errors import _extract_human_error_message, _human_exception_error, _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.providers.openai_chat import call_llm_api
from ai_runtime.providers.openai_responses import _convert_messages_for_responses_api
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage

async def call_xai_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                       input_token_fallback=None,
                       pdf_error_metadata=None,
                       prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                       llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                       pending_attachment_refs: Optional[list[str]] = None,
                       strip_device_action_blocks: bool = False,
                       billing_reservation_id: str | None = None):
    api_url = "https://api.x.ai/v1/chat/completions"
    api_key = user_api_key or xai_key  # Use user's key if provided

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
                                  billing_reservation_id: str | None = None):
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

    error_yielded = False
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

    logger.info(f"call_xai_responses_api -> model: {model}, tools: {len(tools) if tools else 0}")

    # Grok models may need extra time for reasoning
    timeout_seconds = 300
    timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.post(api_url, headers=headers, json=data) as response:
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
                                continue

                            etype = event_type or event_data.get("type", "")

                            # --- Text streaming ---
                            if etype in ("response.output_text.delta", "response.text.delta"):
                                delta = event_data.get("delta", "")
                                if delta:
                                    content += delta
                                    yield f"data: {orjson.dumps({'content': delta}).decode()}\n\n"

                            # --- Web search status ---
                            elif etype == "response.web_search_call.in_progress":
                                yield f"data: {orjson.dumps({'searching': True}).decode()}\n\n"

                            elif etype == "response.web_search_call.searching":
                                pass

                            elif etype == "response.web_search_call.completed":
                                yield f"data: {orjson.dumps({'searching': False}).decode()}\n\n"

                            # --- Function call handling ---
                            elif etype == "response.output_item.added":
                                item = event_data.get("item", {})
                                if item.get("type") == "function_call":
                                    function_name = item.get("name", "")
                                    tool_call_id = item.get("call_id", "")
                                    function_arguments = ""
                                    # xAI may send complete arguments in one chunk
                                    if item.get("arguments"):
                                        function_arguments = item["arguments"]

                            elif etype == "response.function_call_arguments.delta":
                                function_arguments += event_data.get("delta", "")

                            elif etype == "response.function_call_arguments.done":
                                pass

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
                                usage = resp.get("usage", {})
                                input_tokens = usage.get("input_tokens", 0)
                                output_tokens = usage.get("output_tokens", 0)
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
                            elif etype == "response.failed":
                                error_info = event_data.get("response", {}).get("error", {})
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

        except asyncio.TimeoutError as exc:
            error_message = f"[call_xai_responses_api] Request timed out after {timeout_seconds}s for model {model}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except aiohttp.ClientError as exc:
            error_message = f"[call_xai_responses_api] Network error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

        except Exception as exc:
            error_message = f"[call_xai_responses_api] Unexpected error: {str(exc)}"
            logger.error(error_message)
            human_msg = _human_exception_error(exc, "xAI (Grok)")
            await record_provider_error_for_label("xAI (Grok)", message=human_msg, exception=exc, model=model, byok=byok)
            yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', human_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            error_yielded = True

    # Emit citations if any were collected
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
    if function_name and save_to_db:
        try:
            parsed_args = orjson.loads(function_arguments) if function_arguments else {}
        except orjson.JSONDecodeError:
            logger.error(f"[call_xai_responses_api] Failed to parse tool arguments: {function_arguments}")
            parsed_args = {}

        logger.info(f"[call_xai_responses_api] Tool call detected: {function_name}")
        logger.debug(f"[call_xai_responses_api] Tool call args: {parsed_args}")

        await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
        yield f"data: {orjson.dumps({'tool_call': {'name': function_name, 'arguments': parsed_args, 'id': tool_call_id, '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}}).decode()}\n\n"
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
            await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
            citations_data = orjson.dumps(citations).decode() if citations else None
            user_message_id, bot_message_id = await save_content_to_db(content, input_tokens, output_tokens, total_tokens, conversation_id, current_user.id, model, user_message=user_message,
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
            await record_provider_success_for_label("xAI (Grok)", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("xAI (Grok)", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('xAI (Grok)', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"
