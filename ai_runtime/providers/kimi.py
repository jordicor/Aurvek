from ai_runtime.dependencies import *
from ai_runtime.providers.openai_chat import call_llm_api
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection


def _kimi_uses_fixed_temperature(model: str) -> bool:
    normalized = (model or "").lower().replace("_", "-")
    return normalized.startswith("kimi-k2.") or normalized.startswith("kimi-k2-")


def _kimi_reasoning_payload(
    reasoning_selection: ReasoningSelection | dict | str | None,
) -> dict | None:
    """Use Kimi's reasoning toggle and, where requested, its effort field."""

    if reasoning_selection is None:
        return None
    selection = parse_reasoning_selection(reasoning_selection)
    if selection.mode == "default":
        return None
    if selection.mode == "off":
        return {"thinking": {"type": "disabled"}}
    if selection.mode == "auto":
        return {"thinking": {"type": "enabled"}}
    payload = {"thinking": {"type": "enabled"}}
    if selection.mode == "custom":
        payload["thinking"]["budget_tokens"] = selection.budget_tokens
    else:
        payload["reasoning_effort"] = selection.mode
    return payload


async def call_kimi_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                        input_token_fallback=None,
                        pdf_error_metadata=None,
                        prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                        llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                        pending_attachment_refs: Optional[list[str]] = None,
                        strip_device_action_blocks: bool = False,
                        billing_reservation_id: str | None = None,
                        reasoning_selection: ReasoningSelection | dict | str | None = None):
    api_url = "https://api.moonshot.ai/v1/chat/completions"
    api_key = user_api_key or moonshot_key
    if not api_key:
        raise ValueError("Kimi API key not configured. Set MOONSHOT_API_KEY in .env")

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
        "Kimi",
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
        extra_body=_kimi_reasoning_payload(reasoning_selection),
        omit_temperature=_kimi_uses_fixed_temperature(model),
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
    ):
        yield chunk
