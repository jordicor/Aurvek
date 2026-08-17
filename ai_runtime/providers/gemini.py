from ai_runtime.dependencies import *
from ai_runtime.config import _log_truncated_response
from ai_runtime.errors import _provider_error_payload
from ai_runtime.persistence.messages import persistence_error_payload, save_content_to_db
from ai_runtime.tooling.citations import build_citation_event
from ai_runtime.provider_health import record_provider_error_for_label, record_provider_success_for_label
from billing.usage_reservations import accumulate_ai_provider_call_usage

async def call_gemini_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                          input_token_fallback=None,
                          pdf_error_metadata=None,
                          prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                          llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                          pending_attachment_refs: Optional[list[str]] = None,
                          strip_device_action_blocks: bool = False,
                          billing_reservation_id: str | None = None):
    global stop_signals
    logger.info("Entering call_gemini_api")
    user_id = current_user.id
    error_yielded = False

    # Determine API key: user's custom key or global
    api_key = user_api_key if user_api_key else gemini_key
    client = google_genai.Client(api_key=api_key)
    if user_api_key:
        logger.info("Using user's custom Google AI API key")

    # Build config
    config = genai_types.GenerateContentConfig(
        system_instruction=prompt,
        temperature=temperature,
        max_output_tokens=max_tokens,
    )

    # Add tools: google_search (native web search) and/or function declarations
    if web_search_mode == 'native':
        tools_list = [genai_types.Tool(google_search=genai_types.GoogleSearch())]
        if tools:
            tools_list.append(genai_types.Tool(function_declarations=tools))
            config.automatic_function_calling = genai_types.AutomaticFunctionCallingConfig(disable=True)
        config.tools = tools_list
        logger.info(f"[call_gemini_api] - Native web search enabled with google_search tool{f' + {len(tools)} function declarations' if tools else ''}")
    elif tools:
        config.tools = [genai_types.Tool(function_declarations=tools)]
        config.automatic_function_calling = genai_types.AutomaticFunctionCallingConfig(disable=True)
        logger.info(f"[call_gemini_api] - Initialized with {len(tools)} tool declarations")

    # Build contents from messages (can be string or structured Content objects)
    contents = messages

    # Generate response
    content = ""
    input_tokens = output_tokens = total_tokens = 0
    function_call_detected = None
    last_chunk = None
    citations = []

    try:
        async for chunk in await client.aio.models.generate_content_stream(
            model=model,
            contents=contents,
            config=config,
        ):
            last_chunk = chunk

            if stop_signals.get(conversation_id):
                logger.info("Stop signal received, exiting Gemini API call loop.")
                break

            # Check for safety blocks
            if chunk.prompt_feedback and chunk.prompt_feedback.block_reason:
                content = "\n\n*Sorry, but I cannot provide a response to that request. Please try rephrasing your question.*"
                yield f"data: {orjson.dumps({'content': content}).decode()}\n\n"
                break

            # Check for function calls
            if chunk.candidates:
                for candidate in chunk.candidates:
                    if candidate.content and candidate.content.parts:
                        for part in candidate.content.parts:
                            if part.function_call:
                                fc = part.function_call
                                function_call_detected = {
                                    'name': fc.name,
                                    'arguments': dict(fc.args) if fc.args else {}
                                }
                                logger.info(f"[call_gemini_api] - Function call detected: {fc.name}")
                                break
                    if function_call_detected:
                        break

            if function_call_detected:
                break

            # Process text
            if chunk.text:
                content += chunk.text
                yield f"data: {orjson.dumps({'content': chunk.text}).decode()}\n\n"

        # Get real token usage from the last chunk if available
        if last_chunk and last_chunk.usage_metadata:
            input_tokens = last_chunk.usage_metadata.prompt_token_count or 0
            output_tokens = last_chunk.usage_metadata.candidates_token_count or 0
            total_tokens = last_chunk.usage_metadata.total_token_count or 0
        else:
            input_tokens = 0
            output_tokens = estimate_message_tokens(content)
            total_tokens = input_tokens + output_tokens

        if last_chunk and last_chunk.candidates:
            finish_reason = getattr(last_chunk.candidates[0], "finish_reason", None)
            finish_reason_text = getattr(finish_reason, "name", None) or str(finish_reason or "")
            if "MAX_TOKENS" in finish_reason_text.upper():
                _log_truncated_response("Gemini", model, conversation_id, llm_id, finish_reason_text, max_tokens)

        # Extract grounding metadata for native web search (Phase 3)
        if web_search_mode == 'native' and last_chunk and last_chunk.candidates:
            candidate = last_chunk.candidates[0]
            grounding_meta = getattr(candidate, 'grounding_metadata', None)
            if grounding_meta:
                citations = []
                search_queries = grounding_meta.web_search_queries or []
                chunks = grounding_meta.grounding_chunks or []
                supports = grounding_meta.grounding_supports or []

                # Map grounding_supports (cited text segments) to their source chunks
                for support in supports:
                    seg = support.segment
                    chunk_indices = support.grounding_chunk_indices or []
                    for idx in chunk_indices:
                        if idx < len(chunks) and chunks[idx].web:
                            citations.append({
                                "url": chunks[idx].web.uri or "",
                                "title": chunks[idx].web.title or "",
                                "cited_text": seg.text or "",
                                "start_index": seg.start_index,
                                "end_index": seg.end_index,
                            })

                # Add source chunks not already cited inline
                cited_urls = {c["url"] for c in citations}
                for chunk in chunks:
                    if chunk.web and chunk.web.uri and chunk.web.uri not in cited_urls:
                        citations.append({"url": chunk.web.uri, "title": chunk.web.title or ""})

                # Google Search widget HTML (mandatory per ToS)
                widget_html = None
                sep = getattr(grounding_meta, 'search_entry_point', None)
                if sep:
                    widget_html = getattr(sep, 'rendered_content', None)

                if citations:
                    yield build_citation_event(citations, search_queries or None, widget_html)
                    logger.info(f"[call_gemini_api] - Native search: {len(citations)} citations from {len(search_queries)} queries")

    except Exception as e:
        logger.error(f"[call_gemini_api] - Error calling Gemini API: {e}")
        await record_provider_error_for_label("Gemini", message=str(e), exception=e, model=model, byok=byok)
        yield f"data: {orjson.dumps(_provider_error_payload('Gemini', str(e), user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
        error_yielded = True

    billing_input_tokens = input_tokens
    billing_output_tokens = output_tokens
    if (
        billing_reservation_id
        and save_to_db
        and (content or function_call_detected or input_tokens or output_tokens)
    ):
        billing_input_tokens, billing_output_tokens = (
            await accumulate_ai_provider_call_usage(
                reservation_id=billing_reservation_id,
                user_id=user_id,
                reported_input_tokens=input_tokens,
                reported_output_tokens=output_tokens,
                input_payload=(prompt, messages),
                output_payload=(content, function_call_detected),
                input_token_fallback=input_token_fallback,
                output_token_cap=max_tokens,
                llm_id=llm_id,
                model=model,
                prompt_id=prompt_id,
                byok=byok,
            )
        )

    # Handle function calls (skip when save_to_db=False, i.e. Multi-AI mode)
    if function_call_detected and save_to_db:
        logger.info(f"[call_gemini_api] - Tool call: {function_call_detected['name']}")
        logger.debug(f"[call_gemini_api] - Tool call args: {function_call_detected['arguments']}")
        await record_provider_success_for_label("Gemini", model=model, byok=byok)
        yield f"data: {orjson.dumps({'tool_call': {'name': function_call_detected['name'], 'arguments': function_call_detected['arguments'], 'id': '', '_billing_usage': {'input_tokens': billing_input_tokens, 'output_tokens': billing_output_tokens}}}).decode()}\n\n"
        yield f"data: {orjson.dumps({'tool_call_pending': True}).decode()}\n\n"
        return

    if save_to_db:
        was_stopped = stop_signals.get(conversation_id, False)
        if not content.strip():
            if was_stopped:
                logger.info(f"User stopped stream before content for conversation {conversation_id}. Skipping save.")
            else:
                logger.warning(f"Empty bot response for conversation {conversation_id}, user {user_id}. "
                               f"Provider: gemini. Not saving to DB.")
                if not error_yielded:
                    await record_provider_error_for_label("Gemini", message="empty response", model=model, byok=byok)
                    empty_msg = "The AI returned an empty response. Please try again."
                    yield f"data: {orjson.dumps(_provider_error_payload('Gemini', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            return
        else:
            try:
                await record_provider_success_for_label("Gemini", model=model, byok=byok)
                citations_data = orjson.dumps(citations).decode() if citations else None
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
            except Exception:
                logger.exception("[call_gemini_api] - Error saving content to database")
                yield f"data: {orjson.dumps(persistence_error_payload()).decode()}\n\n"
                return

        yield content.strip()
    else:
        if content.strip():
            await record_provider_success_for_label("Gemini", model=model, byok=byok)
        elif not error_yielded:
            await record_provider_error_for_label("Gemini", message="empty response", model=model, byok=byok)
            empty_msg = "The AI returned an empty response. Please try again."
            yield f"data: {orjson.dumps(_provider_error_payload('Gemini', empty_msg, user_message, pdf_error_metadata, current_user, conversation_id)).decode()}\n\n"
            yield "data: [DONE]\n\n"
            return
        yield f"data: {orjson.dumps({'token_info': True, 'input_tokens': input_tokens, 'output_tokens': output_tokens}).decode()}\n\n"
        yield "data: [DONE]\n\n"
