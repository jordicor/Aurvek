from ai_runtime.dependencies import *

# Caches and runtime provider settings
model_token_cost_cache = {}
NATIVE_SEARCH_PROVIDERS = {"Claude", "GPT", "xAI"}
LIVE_VOICE_INPUT_ORIGINS = {"phone.live_call", "web.live_voice"}


def limit_live_voice_output(max_tokens: int) -> int:
    """Keep each spoken intervention's output and reservation bounded."""
    return min(max_tokens, 1024)

def safe_log_headers(headers: dict) -> dict:
    """Return a copy of headers with sensitive values masked."""
    sensitive_keys = {'x-api-key', 'authorization', 'x-goog-api-key'}
    safe = {}
    for k, v in headers.items():
        if k.lower() in sensitive_keys and isinstance(v, str) and len(v) > 8:
            safe[k] = f"{v[:4]}****"
        else:
            safe[k] = v
    return safe


def _positive_int(value, default: int | None = None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _model_output_cap(max_output_tokens) -> tuple[int, bool]:
    cap = _positive_int(max_output_tokens)
    if cap:
        return cap, False
    return int(MAX_TOKENS), True

def _log_output_limit_decision(
    *,
    source: str,
    conversation_id: int,
    llm_id,
    machine: str,
    model: str,
    max_output_tokens,
    fallback_used: bool,
    final_limit: int,
    balance_limited: bool,
    current_balance=None,
):
    logger.info(
        "[output_limit] source=%s conversation_id=%s llm_id=%s machine=%s model=%s "
        "catalog_max_output_tokens=%s fallback_used=%s final_limit=%s balance_limited=%s balance=%s",
        source,
        conversation_id,
        llm_id,
        machine,
        model,
        max_output_tokens,
        fallback_used,
        final_limit,
        balance_limited,
        current_balance,
    )


def _log_truncated_response(provider: str, model: str, conversation_id: int, llm_id, reason: str, max_tokens: int):
    logger.warning(
        "[output_truncated] provider=%s model=%s conversation_id=%s llm_id=%s reason=%s request_limit=%s",
        provider,
        model,
        conversation_id,
        llm_id,
        reason,
        max_tokens,
    )

def _is_gpt5_model(model: str) -> bool:
    """Check if a model is GPT-5 family (requires max_completion_tokens, no custom temperature)."""
    return model.startswith("gpt-5")
