# tools/perplexity.py

import os
import orjson
import aiohttp
import math
from dataclasses import dataclass
from dotenv import load_dotenv
from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    accumulate_ai_reservation_usage,
    estimate_customer_charge_from_api_cost,
    estimate_structured_billing_tokens,
    estimate_structured_usage_tokens,
    mark_ai_reservation_provider_started,
    refund_fixed_usage,
    reserve_ai_usage,
    settle_ai_reservation_components,
)
from database import get_db_connection
from log_config import logger
from tools import register_tool, register_function_handler

load_dotenv()

PERPLEXITY_API_KEY = os.getenv('PERPLEXITY_API_KEY')
PERPLEXITY_MODEL = "sonar-pro"
PERPLEXITY_INPUT_COST_PER_MILLION = float(
    os.getenv("PERPLEXITY_INPUT_COST_PER_MILLION", "3")
)
PERPLEXITY_OUTPUT_COST_PER_MILLION = float(
    os.getenv("PERPLEXITY_OUTPUT_COST_PER_MILLION", "15")
)
if not all(
    math.isfinite(value) and value >= 0
    for value in (
        PERPLEXITY_INPUT_COST_PER_MILLION,
        PERPLEXITY_OUTPUT_COST_PER_MILLION,
    )
):
    raise RuntimeError("Perplexity token prices must be finite and non-negative")
PERPLEXITY_MAX_OUTPUT_TOKENS = min(
    128_000,
    max(1, int(os.getenv("PERPLEXITY_MAX_OUTPUT_TOKENS", "4096"))),
)
PERPLEXITY_SEARCH_CONTEXT_SIZE = os.getenv(
    "PERPLEXITY_SEARCH_CONTEXT_SIZE",
    "low",
).strip().lower()
if PERPLEXITY_SEARCH_CONTEXT_SIZE not in {"low", "medium", "high"}:
    PERPLEXITY_SEARCH_CONTEXT_SIZE = "low"

_PERPLEXITY_REQUEST_FEES = {
    "low": 0.006,
    "medium": 0.010,
    "high": 0.014,
}


@dataclass(frozen=True)
class PerplexityResult:
    content: str
    input_tokens: int
    output_tokens: int
    api_cost: float


class PerplexityProviderError(RuntimeError):
    """An HTTP failure can still report already incurred provider usage."""

    def __init__(self, status: int, *, usage: PerplexityResult | None = None):
        super().__init__(f"Perplexity API error {status}")
        self.usage = usage
        self.rejected = status in {400, 401, 403, 404, 413, 422, 429} and usage is None


def _request_fee() -> float:
    configured = os.getenv("PERPLEXITY_REQUEST_FEE")
    if configured is None:
        return _PERPLEXITY_REQUEST_FEES[PERPLEXITY_SEARCH_CONTEXT_SIZE]
    try:
        value = float(configured)
    except (TypeError, ValueError):
        return _PERPLEXITY_REQUEST_FEES[PERPLEXITY_SEARCH_CONTEXT_SIZE]
    if not math.isfinite(value) or value < 0:
        return _PERPLEXITY_REQUEST_FEES[PERPLEXITY_SEARCH_CONTEXT_SIZE]
    return value


def _research_messages(query: str) -> list[dict]:
    return [
        {
            "content": (
                "You are a research assistant. Provide comprehensive, factual "
                "search results with sources and citations. The user's AI "
                "assistant will use your output to formulate a final answer. "
                "Be thorough and include all relevant data."
            ),
            "role": "system",
        },
        {"content": query, "role": "user"},
    ]


def _research_payload(query: str) -> dict:
    return {
        "messages": _research_messages(query),
        "model": PERPLEXITY_MODEL,
        "max_tokens": PERPLEXITY_MAX_OUTPUT_TOKENS,
        "stream": False,
        "return_citations": True,
        "return_images": False,
        "return_related_questions": False,
        "web_search_options": {
            "search_context_size": PERPLEXITY_SEARCH_CONTEXT_SIZE,
        },
        "temperature": 0.7,
    }


def _non_negative_number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _has_reported_perplexity_usage(usage: dict) -> bool:
    """Return whether a malformed response still proves billable work."""
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if field in usage and _non_negative_number(usage.get(field)) is not None:
            return True
    cost_details = usage.get("cost")
    if not isinstance(cost_details, dict):
        return False
    return any(
        field in cost_details
        and _non_negative_number(cost_details.get(field)) is not None
        for field in ("request_cost", "total_cost")
    )


def _parse_perplexity_response(data: dict, payload: dict) -> PerplexityResult:
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    choices = data.get("choices", [])
    content = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            content = message["content"]
    has_content = bool(content.strip())
    if not has_content and not _has_reported_perplexity_usage(usage):
        raise RuntimeError("Perplexity returned empty or malformed response")

    reported_input = _non_negative_number(usage.get("prompt_tokens"))
    reported_output = _non_negative_number(usage.get("completion_tokens"))
    input_tokens = (
        int(reported_input)
        if reported_input is not None
        else estimate_structured_usage_tokens(payload["messages"])
    )
    if reported_output is not None:
        output_tokens = int(reported_output)
    elif has_content:
        output_tokens = min(
            PERPLEXITY_MAX_OUTPUT_TOKENS,
            estimate_structured_usage_tokens(content),
        )
    else:
        output_tokens = 0

    token_cost = (
        input_tokens * PERPLEXITY_INPUT_COST_PER_MILLION
        + output_tokens * PERPLEXITY_OUTPUT_COST_PER_MILLION
    ) / 1_000_000
    cost_details = (
        usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
    )
    reported_request_cost = _non_negative_number(
        cost_details.get("request_cost")
    )
    request_cost = (
        reported_request_cost
        if reported_request_cost is not None
        else _request_fee()
    )
    reported_total_cost = _non_negative_number(cost_details.get("total_cost"))
    calculated_cost = token_cost + request_cost
    api_cost = max(calculated_cost, reported_total_cost or 0.0)
    return PerplexityResult(
        content=content,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        api_cost=api_cost,
    )

async def _get_perplexity_result(query: str) -> PerplexityResult:
    """Call Perplexity and return normalized content, usage, and provider cost.

    Used by the second-pass flow: the AI calls query_perplexity as a tool,
    we fetch the search results here, then feed them back to the original AI
    so it can formulate its own answer with personality and context.
    """
    if not PERPLEXITY_API_KEY:
        raise RuntimeError("PERPLEXITY_API_KEY not configured")

    url = "https://api.perplexity.ai/chat/completions"
    payload = _research_payload(query)
    headers = {
        "Authorization": f"Bearer {PERPLEXITY_API_KEY}",
        "Content-Type": "application/json"
    }

    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                partial = None
                try:
                    error_data = orjson.loads(error_text)
                    reported = error_data.get("usage") if isinstance(error_data, dict) else None
                    if isinstance(reported, dict) and _has_reported_perplexity_usage(reported):
                        partial = _parse_perplexity_response({"usage": reported}, payload)
                except (orjson.JSONDecodeError, TypeError, ValueError):
                    pass
                raise PerplexityProviderError(response.status, usage=partial)

            data = await response.json()
            return _parse_perplexity_response(data, payload)


async def get_billed_perplexity_result(
    query: str,
    *,
    user_id: int,
    prompt_id: int | None,
) -> str:
    """Reserve and settle one Sonar Pro request before exposing its result."""
    if not PERPLEXITY_API_KEY:
        raise RuntimeError("PERPLEXITY_API_KEY not configured")

    payload = _research_payload(query)
    maximum_input_tokens = estimate_structured_billing_tokens(
        payload["messages"]
    )
    maximum_api_cost = (
        maximum_input_tokens * PERPLEXITY_INPUT_COST_PER_MILLION
        + PERPLEXITY_MAX_OUTPUT_TOKENS
        * PERPLEXITY_OUTPUT_COST_PER_MILLION
    ) / 1_000_000 + _request_fee()
    maximum_customer_charge = await estimate_customer_charge_from_api_cost(
        user_id=int(user_id),
        prompt_id=prompt_id,
        api_cost=maximum_api_cost,
        maximum_tokens=maximum_input_tokens + PERPLEXITY_MAX_OUTPUT_TOKENS,
    )
    reservation_id = await reserve_ai_usage(
        user_id=int(user_id),
        maximum_amount=maximum_customer_charge,
    )
    provider_started = False
    provider_rejected = False

    async def record_usage(result):
        component = {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "input_cost_per_million": PERPLEXITY_INPUT_COST_PER_MILLION,
            "output_cost_per_million": PERPLEXITY_OUTPUT_COST_PER_MILLION,
            "prompt_id": prompt_id,
            "byok": False,
            "override_api_cost": result.api_cost,
        }
        await accumulate_ai_reservation_usage(
            reservation_id=reservation_id,
            user_id=int(user_id),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            component=component,
        )
        return component

    try:
        from integrations.applications.billing import (
            current_application_operation, revalidate_application_operation,
        )
        operation = current_application_operation(user_id)
        if operation is not None:
            await revalidate_application_operation(operation)
        if not await mark_ai_reservation_provider_started(
                reservation_id=reservation_id, user_id=int(user_id)):
            raise BillingReservationError("Perplexity billing reservation is not active")
        # Marking the hold can wait for SQLite. Check again immediately before
        # transport; a revoked admission has not performed provider work yet.
        if operation is not None:
            await revalidate_application_operation(operation)
        provider_started = True
        result = await _get_perplexity_result(query)
        component = await record_usage(result)
        settled = await settle_ai_reservation_components(
            reservation_id=reservation_id,
            user_id=int(user_id),
            prompt_id=prompt_id,
            components=[component],
        )
        if not settled:
            raise BillingReservationError(
                "Perplexity billing reservation is not active"
            )
        if not result.content.strip():
            raise RuntimeError("Perplexity returned empty or malformed response")
        return result.content
    except PerplexityProviderError as exc:
        provider_rejected = exc.rejected
        if exc.usage is not None:
            await record_usage(exc.usage)
        raise
    finally:
        # An ambiguous started request is not proof of unused credit. Native
        # reconciliation can settle reported partial usage or retain unknowns.
        if reservation_id and (not provider_started or provider_rejected):
            try:
                await refund_fixed_usage(reservation_id)
            except BillingReservationError:
                logger.exception(
                    "Could not refund failed Perplexity reservation %s",
                    reservation_id,
                )


async def _resolve_conversation_prompt_id(
    conversation_id: int,
    user_id: int,
) -> int | None:
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT COALESCE(c.role_id, ud.current_prompt_id)
            FROM CONVERSATIONS AS c
            JOIN USER_DETAILS AS ud ON ud.user_id = c.user_id
            WHERE c.id = ? AND c.user_id = ?
            """,
            (int(conversation_id), int(user_id)),
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("Conversation is unavailable")
    return int(row[0]) if row[0] is not None else None


async def handle_perplexity_query(
    function_arguments,
    messages,
    model,
    temperature,
    max_tokens,
    content,
    conversation_id,
    current_user,
    request,
    input_tokens,
    output_tokens,
    total_tokens,
    message_id,
    user_id,
    client,
    prompt,
    user_message=None,
):
    """Compatibility handler; all provider work delegates to billed Sonar."""
    del (
        messages,
        model,
        temperature,
        max_tokens,
        content,
        request,
        input_tokens,
        output_tokens,
        total_tokens,
        message_id,
        client,
        prompt,
        user_message,
    )
    authenticated_user_id = int(current_user.id)
    if int(user_id) != authenticated_user_id:
        logger.warning(
            "Rejected Perplexity handler user mismatch for conversation %s",
            conversation_id,
        )
        yield f"data: {orjson.dumps({'content': 'Web search is unavailable', 'is_error': True}).decode()}\n\n"
        return

    query = (
        str(function_arguments.get("query") or "")
        if isinstance(function_arguments, dict)
        else ""
    )
    if not query.strip():
        yield f"data: {orjson.dumps({'content': 'Web search query was empty', 'is_error': True}).decode()}\n\n"
        return

    try:
        prompt_id = await _resolve_conversation_prompt_id(
            conversation_id,
            authenticated_user_id,
        )
        result = await get_billed_perplexity_result(
            query,
            user_id=authenticated_user_id,
            prompt_id=prompt_id,
        )
    except InsufficientBalanceError:
        yield f"data: {orjson.dumps({'content': 'Insufficient balance for web search', 'is_error': True}).decode()}\n\n"
        return
    except Exception:
        logger.exception(
            "Billed Perplexity handler failed for conversation %s",
            conversation_id,
        )
        yield f"data: {orjson.dumps({'content': 'Web search is temporarily unavailable', 'is_error': True}).decode()}\n\n"
        return

    yield f"data: {orjson.dumps({'content': result}).decode()}\n\n"

# Register the tool for the semantic router
register_tool({
    "type": "function",
    "function": {
        "name": "query_perplexity",
        "description": "Use this tool for up-to-date or real-time internet searches. Formulate a detailed natural language query for best results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Detailed natural language query for semantic search. Clearly specify what information you're looking for and what you expect to obtain."
                }
            },
            "required": ["query"],
            "additionalProperties": False
        }
    },
    "strict": True
})

# Register the function handler
register_function_handler("query_perplexity", handle_perplexity_query)
