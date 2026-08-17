from ai_runtime.dependencies import *
from ai_runtime.providers.claude_capabilities import claude_omits_temperature
from ai_runtime.providers.openai_chat import call_llm_api

async def call_openrouter_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                              input_token_fallback=None,
                              pdf_error_metadata=None,
                              prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                              llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False, api_model=None,
                              pending_attachment_refs: Optional[list[str]] = None,
                              strip_device_action_blocks: bool = False,
                              billing_reservation_id: str | None = None):
    """
    Call OpenRouter unified API - 100% OpenAI compatible.

    Supports 300+ models including:
    - meta-llama/llama-3.3-70b-instruct
    - deepseek/deepseek-r1
    - deepseek/deepseek-chat-v3-0324
    - mistralai/mistral-large-2411
    - qwen/qwen-2.5-72b-instruct
    - cohere/command-r-plus
    - And many more...

    Model names use format: provider/model-name
    """
    api_url = "https://openrouter.ai/api/v1/chat/completions"
    api_key = user_api_key or openrouter_key

    if not api_key:
        raise ValueError("OpenRouter API key not configured. Set OPENROUTER_API_KEY in .env")

    # Extended timeout for reasoning models (DeepSeek R1, etc.)
    model_lower = model.lower()
    if "deepseek-r1" in model_lower or "reasoning" in model_lower:
        custom_timeout = 300  # 5 minutes for reasoning models
    else:
        custom_timeout = 180  # 3 minutes for standard models

    # OpenRouter recommended headers for tracking
    extra_headers = {
        "HTTP-Referer": f"https://{os.getenv('PRIMARY_APP_DOMAIN', 'localhost')}",
        "X-Title": "AURVEK AI Chat"
    }

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
        "OpenRouter",
        user_message=user_message,
        input_token_fallback=input_token_fallback,
        pdf_error_metadata=pdf_error_metadata,
        extra_headers=extra_headers,
        custom_timeout=custom_timeout,
        tools=tools,
        prompt_id=prompt_id,
        watchdog_config=watchdog_config,
        watchdog_hint_active=watchdog_hint_active,
        watchdog_hint_eval_id=watchdog_hint_eval_id,
        llm_id=llm_id,
        save_to_db=save_to_db,
        web_search_mode=web_search_mode,
        byok=byok,
        api_model=api_model,
        omit_temperature=claude_omits_temperature(api_model or model),
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
    ):
        yield chunk
