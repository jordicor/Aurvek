from ai_runtime.dependencies import *
from ai_runtime.providers.openai_chat import call_llm_api
from ai_runtime.reasoning import ReasoningSelection, parse_reasoning_selection


def _minimax_reasoning_payload(
    reasoning_selection: ReasoningSelection | dict | str | None,
) -> dict | None:
    """MiniMax's thinking toggle; reasoning_split is deliberately separate."""

    if reasoning_selection is None:
        return None
    selection = parse_reasoning_selection(reasoning_selection)
    if selection.mode == "default":
        return None
    if selection.mode == "off":
        return {"type": "disabled"}
    return {"type": "adaptive"}


async def call_minimax_api(messages, model, temperature, max_tokens, prompt, conversation_id, current_user, request, user_message=None, user_api_key=None, tools=None,
                           input_token_fallback=None,
                           pdf_error_metadata=None,
                           prompt_id=None, watchdog_config=None, watchdog_hint_active=False, watchdog_hint_eval_id=None,
                           llm_id=None, save_to_db: bool = True, web_search_mode=None, byok: bool = False,
                           pending_attachment_refs: Optional[list[str]] = None,
                           strip_device_action_blocks: bool = False,
                           billing_reservation_id: str | None = None,
                           reasoning_selection: ReasoningSelection | dict | str | None = None):
    api_url = "https://api.minimax.io/v1/chat/completions"
    api_key = user_api_key or minimax_key
    if not api_key:
        raise ValueError("MiniMax API key not configured. Set MINIMAX_API_KEY in .env")

    extra_body = {"reasoning_split": True}
    thinking = _minimax_reasoning_payload(reasoning_selection)
    if thinking is not None:
        extra_body["thinking"] = thinking

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
        "MiniMax",
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
        extra_body=extra_body,
        use_max_completion_tokens=True,
        pending_attachment_refs=pending_attachment_refs,
        strip_device_action_blocks=strip_device_action_blocks,
        billing_reservation_id=billing_reservation_id,
    ):
        yield chunk
