"""Serialize variable-cost usage and reserve fixed provider costs safely."""

from __future__ import annotations

import asyncio
import math
import orjson
import secrets
import sqlite3
import time
from contextvars import ContextVar
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from starlette.responses import StreamingResponse

from common import record_daily_usage
from database import (
    DB_MAX_RETRIES,
    DB_RETRY_DELAY_BASE,
    get_db_connection,
    is_lock_error,
)
from log_config import logger


class BillingReservationError(RuntimeError):
    """Raised when a cost cannot be reserved or finalized safely."""


class InsufficientBalanceError(BillingReservationError):
    """Raised when the paying account cannot cover a reservation."""


class BillingLimitExceededError(InsufficientBalanceError):
    """Raised when a team member has exhausted their monthly allowance."""


_USAGE_TOTAL_COLUMNS = {
    "image": "total_image_cost",
    "stt": "total_stt_cost",
    "tts": "total_tts_cost",
    "video": None,
    # Telephone components use their own durable per-call ledger.  The normal
    # Aurvek reservation still owns the payer balance, team allowance,
    # SERVICE_USAGE row and aggregate total_cost, but must not guess whether a
    # Twilio/Deepgram/TTS component belongs in one of the legacy narrow totals.
    "phone": None,
}


@dataclass(frozen=True)
class VariableBillingRates:
    input_per_token: float
    output_per_token: float


def _structured_character_weight(value, key: str | None = None) -> int:
    if value is None:
        return 0
    if isinstance(value, bytes):
        return 4096
    if isinstance(value, str):
        prefix = value[:96].lower()
        if ";base64," in prefix or (
            key in {"data", "base64", "image_data", "pdf_data"}
            and len(value) > 1024
        ):
            return 4096
        return len(value.encode("utf-8"))
    if isinstance(value, dict):
        return sum(
            len(str(child_key).encode("utf-8"))
            + _structured_character_weight(child_value, str(child_key).lower())
            for child_key, child_value in value.items()
        )
    if isinstance(value, (list, tuple, set)):
        return sum(_structured_character_weight(item, key) for item in value)
    return len(str(value).encode("utf-8"))


def _structured_has_content(value) -> bool:
    if value is None:
        return False
    if isinstance(value, (str, bytes)):
        return bool(value)
    if isinstance(value, dict):
        return any(_structured_has_content(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_structured_has_content(item) for item in value)
    return True


def estimate_structured_billing_tokens(*values) -> int:
    """Return a deliberately conservative token upper bound for a hold."""
    byte_upper_bound = sum(_structured_character_weight(value) for value in values)
    return max(1, byte_upper_bound + 64)


def estimate_structured_usage_tokens(*values) -> int:
    """Estimate actual tokens when a provider omits usage metadata."""
    byte_weight = sum(_structured_character_weight(value) for value in values)
    return max(1, math.ceil(byte_weight / 4 * 1.1) + 16)


async def get_variable_billing_rates(
    *,
    user_id: int,
    prompt_id: int | None,
    input_cost_per_million: float,
    output_cost_per_million: float,
    byok: bool,
) -> VariableBillingRates:
    """Mirror the linear pricing formula used by ``consume_token``."""
    from common import (
        get_pricing_config,
        get_prompt_pricing_info,
        get_user_referral_info,
    )

    input_api_rate = 0.0 if byok else float(input_cost_per_million or 0) / 1_000_000
    output_api_rate = 0.0 if byok else float(output_cost_per_million or 0) / 1_000_000
    if min(input_api_rate, output_api_rate) < 0 or not all(
        math.isfinite(rate) for rate in (input_api_rate, output_api_rate)
    ):
        raise BillingReservationError("Invalid model billing rates")

    pricing = await get_pricing_config()
    markup_rate = 0.0
    if prompt_id:
        prompt_info = await get_prompt_pricing_info(prompt_id)
        if not prompt_info["is_paid"]:
            margin = float(pricing["margin_free"])
        elif int(prompt_info.get("created_by_user_id") or 0) == int(user_id):
            margin = float(pricing["margin_personal"])
        else:
            margin = float(pricing["margin_paid"])
            markup_rate += float(prompt_info.get("markup_per_mtokens") or 0) / 1_000_000
            referral = await get_user_referral_info(user_id)
            if referral.get("created_by"):
                markup_rate += max(
                    0.0,
                    float(referral.get("referral_markup_per_mtokens") or 0),
                ) / 1_000_000
    else:
        margin = float(pricing["margin_free"])

    input_rate = input_api_rate * (1 + margin) + markup_rate
    output_rate = output_api_rate * (1 + margin) + markup_rate
    if min(input_rate, output_rate) < 0 or not all(
        math.isfinite(rate) for rate in (input_rate, output_rate)
    ):
        raise BillingReservationError("Invalid final billing rates")
    return VariableBillingRates(input_rate, output_rate)


async def estimate_customer_charge_from_api_cost(
    *,
    user_id: int,
    prompt_id: int | None,
    api_cost: float,
    maximum_tokens: int,
) -> float:
    """Apply marketplace margins/markups to a provider-cost upper bound."""
    from common import (
        get_pricing_config,
        get_prompt_pricing_info,
        get_user_referral_info,
    )

    api_cost = max(0.0, float(api_cost or 0))
    pricing = await get_pricing_config()
    markup_per_token = 0.0
    if prompt_id:
        prompt_info = await get_prompt_pricing_info(prompt_id)
        if not prompt_info["is_paid"]:
            margin = float(pricing["margin_free"])
        elif int(prompt_info.get("created_by_user_id") or 0) == int(user_id):
            margin = float(pricing["margin_personal"])
        else:
            margin = float(pricing["margin_paid"])
            markup_per_token += float(prompt_info.get("markup_per_mtokens") or 0) / 1_000_000
            referral = await get_user_referral_info(user_id)
            if referral.get("created_by"):
                markup_per_token += max(
                    0.0,
                    float(referral.get("referral_markup_per_mtokens") or 0),
                ) / 1_000_000
    else:
        margin = float(pricing["margin_free"])
    return api_cost * (1 + margin) + max(0, maximum_tokens) * markup_per_token


class _ReentrantAsyncLock:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: object | None = None
        self._depth = 0

    async def acquire(self, owner: object) -> None:
        if self._owner is owner:
            self._depth += 1
            return
        await self._lock.acquire()
        self._owner = owner
        self._depth = 1

    def release(self, owner: object) -> None:
        if self._owner is not owner:
            raise RuntimeError("Billing lock released by a different logical owner")
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
            self._lock.release()


@dataclass
class _LockEntry:
    lock: _ReentrantAsyncLock
    participants: int = 0


_account_locks: dict[int, _LockEntry] = {}
_billing_owner_context: ContextVar[object | None] = ContextVar(
    "billing_owner_context",
    default=None,
)
_detached_billing_producers: set[asyncio.Task] = set()
_last_stale_reconciliation = 0.0

_STALE_RESERVATION_PREDICATE = """
    status = 'active' AND (
        (purpose = 'image' AND (
            (provider_started_at IS NULL
             AND created_at < datetime('now', '-15 minutes'))
            OR provider_started_at < datetime('now', '-15 minutes')
        ))
        OR (purpose = 'video' AND (
            (provider_started_at IS NULL
             AND created_at < datetime('now', '-15 minutes'))
            OR provider_started_at < datetime('now', '-15 minutes')
        ))
        OR (purpose = 'tts' AND (
            (provider_started_at IS NULL
             AND created_at < datetime('now', '-15 minutes'))
            OR provider_started_at < datetime('now', '-15 minutes')
        ))
        OR (purpose = 'stt'
            AND created_at < datetime('now', '-30 minutes'))
        OR (purpose = 'ai'
            AND created_at < datetime('now', '-12 hours')
            AND (
                provider_started_at IS NULL
                OR COALESCE(TRIM(accumulated_components), '[]') <> '[]'
            ))
    )
"""


@dataclass
class _AccountLease:
    account_id: int
    entry: _LockEntry
    owner: object
    released: bool = False

    async def release(self) -> None:
        if self.released:
            return
        self.entry.lock.release(self.owner)
        self.released = True
        self.entry.participants -= 1
        if self.entry.participants == 0:
            _account_locks.pop(self.account_id, None)


@dataclass
class _StreamLeaseState:
    producer_started: bool = False
    consumer_active: bool = True


class _BillingGuardedStreamingResponse(StreamingResponse):
    """Delegate a stream while guaranteeing its billing lease is released."""

    def __init__(
        self,
        response: StreamingResponse,
        lease: _AccountLease,
        owner: object,
        state: _StreamLeaseState,
    ) -> None:
        self._response = response
        self._lease = lease
        self._owner = owner
        self._state = state
        self.status_code = response.status_code
        self.media_type = response.media_type
        self.background = response.background
        self.raw_headers = list(response.raw_headers)
        self.body_iterator = response.body_iterator

    async def __call__(self, scope, receive, send) -> None:
        owner_token = _billing_owner_context.set(self._owner)
        try:
            await self._response(scope, receive, send)
        finally:
            try:
                if not self._state.producer_started:
                    await self._lease.release()
            finally:
                _billing_owner_context.reset(owner_token)


async def _acquire_account_lease(account_id: int, owner: object) -> _AccountLease:
    normalized_id = int(account_id)

    entry = _account_locks.get(normalized_id)
    if entry is None:
        entry = _LockEntry(lock=_ReentrantAsyncLock())
        _account_locks[normalized_id] = entry
    entry.participants += 1

    try:
        await entry.lock.acquire(owner)
    except BaseException:
        entry.participants -= 1
        if entry.participants == 0:
            _account_locks.pop(normalized_id, None)
        raise
    return _AccountLease(normalized_id, entry, owner)


async def _acquire_user_account_lease(
    user_id: int,
    owner: object,
) -> _AccountLease:
    """Lock the payer, retrying if team ownership changes while waiting."""
    for _ in range(3):
        availability = await get_user_billing_availability(int(user_id))
        lease = await _acquire_account_lease(
            availability["billing_account_id"],
            owner,
        )
        try:
            fresh = await get_user_billing_availability(int(user_id))
        except BaseException:
            await lease.release()
            raise
        if fresh["billing_account_id"] == lease.account_id:
            return lease
        await lease.release()
    raise BillingReservationError("Billing account changed; retry the request")


@asynccontextmanager
async def billing_account_guard(account_id: int):
    """Serialize provider usage for one balance account in this worker.

    This ordering lock is intentionally process-local, and production currently
    runs one Uvicorn worker (``UVICORN_WORKERS=1``). Durable balance reservations
    provide the cross-process monetary guarantee; this lock keeps same-worker
    streams ordered and their fresh preflight deterministic. Reentrant behavior
    is required because a stream can invoke another billable tool such as image
    generation while already holding the account guard.
    """
    owner = _billing_owner_context.get()
    owner_token = None
    if owner is None:
        owner = object()
        owner_token = _billing_owner_context.set(owner)
    lease = None
    try:
        lease = await _acquire_account_lease(account_id, owner)
        yield lease.account_id
    finally:
        if lease is not None:
            await lease.release()
        if owner_token is not None:
            _billing_owner_context.reset(owner_token)


@asynccontextmanager
async def user_billing_guard(user_id: int):
    """Acquire the guard for the account that pays this user's AI usage."""
    owner = _billing_owner_context.get()
    owner_token = None
    if owner is None:
        owner = object()
        owner_token = _billing_owner_context.set(owner)
    lease = None
    try:
        lease = await _acquire_user_account_lease(int(user_id), owner)
        yield lease.account_id
    finally:
        if lease is not None:
            await lease.release()
        if owner_token is not None:
            _billing_owner_context.reset(owner_token)


async def serialize_user_billing_stream(user_id: int, stream):
    """Run an async provider stream to billing completion after disconnect."""
    state = _StreamLeaseState()
    queue: asyncio.Queue = asyncio.Queue(maxsize=16)

    async def publish(kind: str, payload=None) -> None:
        while state.consumer_active:
            try:
                await asyncio.wait_for(queue.put((kind, payload)), timeout=0.25)
                return
            except asyncio.TimeoutError:
                continue

    async def produce() -> None:
        try:
            async with user_billing_guard(user_id):
                async for item in stream:
                    if state.consumer_active:
                        await publish("item", item)
            if state.consumer_active:
                await publish("done")
        except BaseException as exc:
            if state.consumer_active:
                await publish("error", exc)
            elif not isinstance(exc, asyncio.CancelledError):
                logger.exception("Detached billable stream failed")

    state.producer_started = True
    producer_task = asyncio.create_task(produce())
    _detached_billing_producers.add(producer_task)
    producer_task.add_done_callback(_detached_billing_producers.discard)
    try:
        while True:
            kind, payload = await queue.get()
            if kind == "item":
                yield payload
            elif kind == "error":
                raise payload
            else:
                break
    finally:
        state.consumer_active = False


async def serialize_user_billing_response(user_id: int, response_awaitable):
    """Run response preflight under the payer lock and hold it through streaming.

    ``process_save_message`` validates before returning its streaming response.
    The guard is therefore acquired before awaiting that coroutine and then
    associated with its body iterator until persistence, billing, or cancellation.
    """
    owner = _billing_owner_context.get()
    owner_token = None
    if owner is None:
        owner = object()
        owner_token = _billing_owner_context.set(owner)
    try:
        lease = await _acquire_user_account_lease(int(user_id), owner)
        try:
            response = await response_awaitable
        except BaseException:
            await lease.release()
            raise
    except BaseException:
        if owner_token is not None:
            _billing_owner_context.reset(owner_token)
        raise
    if owner_token is not None:
        _billing_owner_context.reset(owner_token)

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        await lease.release()
        return response

    stream_state = _StreamLeaseState()

    async def guarded_body_iterator():
        body_owner_token = _billing_owner_context.set(owner)
        queue: asyncio.Queue = asyncio.Queue(maxsize=16)
        stream_state.producer_started = True

        async def publish_to_consumer(kind: str, payload=None) -> None:
            while stream_state.consumer_active:
                try:
                    await asyncio.wait_for(
                        queue.put((kind, payload)),
                        timeout=0.25,
                    )
                    return
                except asyncio.TimeoutError:
                    continue

        async def produce_and_bill() -> None:
            try:
                async for item in body_iterator:
                    if stream_state.consumer_active:
                        await publish_to_consumer("item", item)
                if stream_state.consumer_active:
                    await publish_to_consumer("done")
            except BaseException as exc:
                if stream_state.consumer_active:
                    await publish_to_consumer("error", exc)
                elif not isinstance(exc, asyncio.CancelledError):
                    logger.exception("Detached billable stream failed")
            finally:
                await lease.release()

        producer_task = asyncio.create_task(produce_and_bill())
        _detached_billing_producers.add(producer_task)
        producer_task.add_done_callback(_detached_billing_producers.discard)
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "item":
                    yield payload
                elif kind == "error":
                    raise payload
                else:
                    break
        finally:
            stream_state.consumer_active = False
            _billing_owner_context.reset(body_owner_token)

    response.body_iterator = guarded_body_iterator()
    return _BillingGuardedStreamingResponse(response, lease, owner, stream_state)


async def get_user_billing_availability(user_id: int) -> dict:
    """Return payer balance constrained by the member's monthly limit."""
    await _maybe_reconcile_stale_usage_reservations()
    user_id = int(user_id)
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT
                COALESCE(member.billing_account_id, member.user_id),
                COALESCE(payer.balance, 0),
                member.billing_account_id,
                member.billing_limit,
                member.billing_limit_action,
                member.billing_current_month_spent,
                member.billing_month_reset_date,
                member.billing_max_limit
            FROM USER_DETAILS AS member
            LEFT JOIN USER_DETAILS AS payer
                ON payer.user_id = COALESCE(
                    member.billing_account_id,
                    member.user_id
                )
            WHERE member.user_id = ?
            """,
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        return {
            "user_id": user_id,
            "billing_account_id": user_id,
            "balance": 0.0,
            "available": 0.0,
            "monthly_remaining": None,
            "billing_limit_action": None,
        }

    payer_id = int(row[0])
    balance = max(0.0, float(row[1] or 0.0))
    is_team_billed = row[2] is not None and int(row[2]) != user_id
    billing_limit = float(row[3]) if row[3] is not None else None
    limit_action = str(row[4] or "block").lower()
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    spent = (
        float(row[5] or 0.0)
        if str(row[6] or "") == current_month
        else 0.0
    )
    max_limit = float(row[7]) if row[7] is not None else None

    monthly_remaining = None
    if is_team_billed and billing_limit is not None:
        if limit_action == "block":
            monthly_remaining = max(0.0, billing_limit - spent)
        elif limit_action == "auto_refill" and max_limit is not None:
            monthly_remaining = max(0.0, max_limit - spent)

    available = min(balance, monthly_remaining) if monthly_remaining is not None else balance
    return {
        "user_id": user_id,
        "billing_account_id": payer_id,
        "balance": balance,
        "available": available,
        "monthly_remaining": monthly_remaining,
        "billing_limit_action": limit_action if is_team_billed else None,
    }


async def revalidate_user_billing(user_id: int, required_amount: float) -> bool:
    """Check fresh payer balance/monthly capacity immediately before a call."""
    required_amount = float(required_amount or 0.0)
    if not math.isfinite(required_amount) or required_amount < 0:
        raise BillingReservationError(
            "Required billing amount must be finite and non-negative"
        )
    if required_amount == 0:
        return True
    availability = await get_user_billing_availability(user_id)
    return availability["available"] + 1e-12 >= required_amount


def _validate_fixed_usage(
    *,
    purpose: str,
    amount: float,
    service_id: int,
    usage_quantity: float,
) -> tuple[str, float, int, float]:
    normalized_purpose = str(purpose or "").strip().lower()
    if normalized_purpose not in _USAGE_TOTAL_COLUMNS:
        raise BillingReservationError(f"Unsupported billing purpose: {purpose}")

    normalized_amount = float(amount)
    normalized_quantity = float(usage_quantity)
    if not math.isfinite(normalized_amount) or normalized_amount <= 0:
        raise BillingReservationError("Reservation amount must be positive and finite")
    if not math.isfinite(normalized_quantity) or normalized_quantity <= 0:
        raise BillingReservationError("Usage quantity must be positive and finite")
    if service_id is None or int(service_id) <= 0:
        raise BillingReservationError(
            f"Service cost configuration is missing for {normalized_purpose}"
        )
    return normalized_purpose, normalized_amount, int(service_id), normalized_quantity


def _calculate_auto_refill_adjustment(
    *,
    current_limit: float,
    requested_spend: float,
    refill_amount: float,
    max_limit: float | None,
) -> tuple[float, float, int]:
    """Return the smallest refill adjustment that covers ``requested_spend``."""
    if requested_spend <= current_limit + 1e-12:
        return current_limit, 0.0, 0
    if not math.isfinite(refill_amount) or refill_amount <= 0:
        raise BillingLimitExceededError("Monthly billing limit reached")
    if max_limit is not None:
        if not math.isfinite(max_limit) or requested_spend > max_limit + 1e-12:
            raise BillingLimitExceededError("Monthly billing limit reached")

    gap = max(0.0, requested_spend - current_limit)
    refill_count = max(1, math.ceil(max(0.0, gap - 1e-12) / refill_amount))
    new_limit = current_limit + refill_count * refill_amount
    if max_limit is not None:
        new_limit = min(new_limit, max_limit)
    if new_limit + 1e-12 < requested_spend:
        raise BillingLimitExceededError("Monthly billing limit reached")
    return new_limit, max(0.0, new_limit - current_limit), refill_count


async def reserve_fixed_usage(
    *,
    user_id: int,
    purpose: str,
    amount: float,
    service_id: int,
    usage_quantity: float,
    billing_account_id: int | None = None,
) -> str:
    """Atomically remove a known provider cost before the provider is called."""
    purpose, amount, service_id, usage_quantity = _validate_fixed_usage(
        purpose=purpose,
        amount=amount,
        service_id=service_id,
        usage_quantity=usage_quantity,
    )
    user_id = int(user_id)
    expected_payer_id = int(billing_account_id) if billing_account_id else None
    return await _reserve_usage_atomic(
        user_id=user_id,
        purpose=purpose,
        amount=amount,
        service_id=service_id,
        usage_quantity=usage_quantity,
        expected_payer_id=expected_payer_id,
    )


async def reserve_ai_usage(*, user_id: int, maximum_amount: float) -> str | None:
    """Reserve a variable AI upper bound before any provider work starts."""
    maximum_amount = float(maximum_amount or 0.0)
    if not math.isfinite(maximum_amount) or maximum_amount < 0:
        raise BillingReservationError("AI reservation amount is invalid")
    if maximum_amount == 0:
        return None

    return await _reserve_usage_atomic(
        user_id=int(user_id),
        purpose="ai",
        amount=maximum_amount,
        service_id=None,
        usage_quantity=None,
        expected_payer_id=None,
    )


async def reserve_ai_provider_call(
    *,
    user_id: int,
    prompt_id: int | None,
    input_payload,
    maximum_output_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
    byok: bool,
) -> tuple[str | None, int]:
    """Reserve one bounded non-streaming provider call."""
    input_tokens = estimate_structured_billing_tokens(input_payload)
    rates = await get_variable_billing_rates(
        user_id=int(user_id),
        prompt_id=prompt_id,
        input_cost_per_million=input_cost_per_million,
        output_cost_per_million=output_cost_per_million,
        byok=bool(byok),
    )
    maximum_amount = (
        input_tokens * rates.input_per_token
        + max(1, int(maximum_output_tokens)) * rates.output_per_token
    )
    reservation_id = await reserve_ai_usage(
        user_id=int(user_id),
        maximum_amount=maximum_amount,
    )
    return reservation_id, input_tokens


async def _reserve_usage_atomic(
    *,
    user_id: int,
    purpose: str,
    amount: float,
    service_id: int | None,
    usage_quantity: float | None,
    expected_payer_id: int | None,
) -> str:
    reservation_id = secrets.token_urlsafe(24)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    last_lock_error = None

    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                member_cursor = await conn.execute(
                    """
                    SELECT billing_account_id, billing_limit,
                           billing_limit_action, billing_current_month_spent,
                           billing_month_reset_date, billing_auto_refill_amount,
                           billing_max_limit
                    FROM USER_DETAILS
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
                member = await member_cursor.fetchone()
                if not member:
                    raise BillingReservationError("Billing account is unavailable")

                payer_id = int(member[0] or user_id)
                if (
                    expected_payer_id is not None
                    and payer_id != expected_payer_id
                ):
                    raise BillingReservationError(
                        "Billing account changed; retry the request"
                    )

                is_team_billed = member[0] is not None and payer_id != user_id
                billing_limit_delta = 0.0
                billing_refill_count_delta = 0
                if is_team_billed:
                    spent = float(member[3] or 0.0)
                    if str(member[4] or "") != current_month:
                        spent = 0.0
                        await conn.execute(
                            """
                            UPDATE USER_DETAILS
                            SET billing_current_month_spent = 0,
                                billing_month_reset_date = ?,
                                billing_auto_refill_count = 0
                            WHERE user_id = ?
                            """,
                            (current_month, user_id),
                        )

                    billing_limit = (
                        float(member[1]) if member[1] is not None else None
                    )
                    limit_action = str(member[2] or "block").lower()
                    requested_spend = spent + amount
                    max_limit = (
                        float(member[6]) if member[6] is not None else None
                    )
                    if (
                        limit_action == "auto_refill"
                        and max_limit is not None
                        and requested_spend > max_limit + 1e-12
                    ):
                        raise BillingLimitExceededError(
                            "Maximum monthly billing limit reached"
                        )
                    if billing_limit is not None and requested_spend > billing_limit:
                        if limit_action == "auto_refill":
                            refill_amount = float(member[5] or 0.0)
                            (
                                new_limit,
                                billing_limit_delta,
                                billing_refill_count_delta,
                            ) = _calculate_auto_refill_adjustment(
                                current_limit=billing_limit,
                                requested_spend=requested_spend,
                                refill_amount=refill_amount,
                                max_limit=max_limit,
                            )
                            await conn.execute(
                                """
                                UPDATE USER_DETAILS
                                SET billing_limit = ?,
                                    billing_auto_refill_count =
                                        COALESCE(billing_auto_refill_count, 0) + ?
                                WHERE user_id = ?
                                """,
                                (new_limit, billing_refill_count_delta, user_id),
                            )
                        elif limit_action != "notify":
                            raise BillingLimitExceededError(
                                "Monthly billing limit reached"
                            )

                result = await conn.execute(
                    """
                    UPDATE USER_DETAILS
                    SET balance = COALESCE(balance, 0) - ?
                    WHERE user_id = ? AND COALESCE(balance, 0) >= ?
                    RETURNING balance
                    """,
                    (amount, payer_id, amount),
                )
                if await result.fetchone() is None:
                    raise InsufficientBalanceError("Insufficient balance")

                if is_team_billed:
                    await conn.execute(
                        """
                        UPDATE USER_DETAILS
                        SET billing_current_month_spent =
                            COALESCE(billing_current_month_spent, 0) + ?
                        WHERE user_id = ?
                        """,
                        (amount, user_id),
                    )

                await conn.execute(
                    """
                    INSERT INTO BILLING_USAGE_RESERVATIONS (
                        id, user_id, billing_account_id, purpose, service_id,
                        usage_quantity, amount, billing_month,
                        billing_limit_delta, billing_refill_count_delta, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        reservation_id,
                        user_id,
                        payer_id,
                        purpose,
                        service_id,
                        usage_quantity,
                        amount,
                        current_month,
                        billing_limit_delta,
                        billing_refill_count_delta,
                    ),
                )
                await conn.commit()
                return reservation_id
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not reserve provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not reserve provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError("Could not reserve provider usage") from last_lock_error


@dataclass(frozen=True)
class AiReservationCredit:
    reservation_id: str
    user_id: int
    billing_account_id: int
    maximum_amount: float
    payer_balance_with_credit: float
    accumulated_input_tokens: int
    accumulated_output_tokens: int
    accumulated_api_cost: float | None


def _accumulated_components_api_cost(raw_components) -> float | None:
    """Return exact raw provider cost, or ``None`` for legacy token-only rows."""
    if raw_components is None or raw_components == "":
        return None
    try:
        components = orjson.loads(raw_components)
    except (orjson.JSONDecodeError, TypeError) as exc:
        raise BillingReservationError(
            "AI usage components are invalid"
        ) from exc
    if not isinstance(components, list):
        raise BillingReservationError("AI usage components are invalid")
    if not components:
        return None

    total = 0.0
    for component in components:
        if not isinstance(component, dict):
            raise BillingReservationError("AI usage component is invalid")
        input_tokens = component.get("input_tokens", 0)
        output_tokens = component.get("output_tokens", 0)
        byok = component.get("byok", False)
        if (
            isinstance(input_tokens, bool)
            or not isinstance(input_tokens, int)
            or input_tokens < 0
            or isinstance(output_tokens, bool)
            or not isinstance(output_tokens, int)
            or output_tokens < 0
            or not isinstance(byok, bool)
        ):
            raise BillingReservationError("AI usage component is invalid")
        try:
            input_rate = float(component.get("input_cost_per_million") or 0.0)
            output_rate = float(component.get("output_cost_per_million") or 0.0)
            override = component.get("override_api_cost")
            if override is not None:
                override = float(override)
        except (TypeError, ValueError) as exc:
            raise BillingReservationError("AI usage component is invalid") from exc
        if min(input_rate, output_rate) < 0 or not all(
            math.isfinite(value) for value in (input_rate, output_rate)
        ):
            raise BillingReservationError("AI usage component rates are invalid")
        if override is not None and (override < 0 or not math.isfinite(override)):
            raise BillingReservationError("AI usage component cost is invalid")

        if byok:
            component_cost = 0.0
        elif override is not None:
            component_cost = override
        else:
            component_cost = (
                input_tokens * input_rate + output_tokens * output_rate
            ) / 1_000_000
        total += component_cost
        if not math.isfinite(total):
            raise BillingReservationError("AI usage component cost is invalid")
    return total


async def accumulate_ai_reservation_usage(
    *,
    reservation_id: str | None,
    user_id: int,
    input_tokens: int,
    output_tokens: int,
    component: dict | None = None,
) -> None:
    """Attach a completed pre-tool provider call to an active AI hold."""
    if not reservation_id:
        raise BillingReservationError("AI billing reservation is missing")
    try:
        input_tokens = int(input_tokens or 0)
        output_tokens = int(output_tokens or 0)
    except (TypeError, ValueError) as exc:
        raise BillingReservationError("AI usage tokens are invalid") from exc
    if input_tokens < 0 or output_tokens < 0:
        raise BillingReservationError("AI usage tokens cannot be negative")

    normalized_component = None
    if component is not None:
        try:
            component_input = max(0, int(component.get("input_tokens") or 0))
            component_output = max(0, int(component.get("output_tokens") or 0))
            component_input_cost = float(
                component.get("input_cost_per_million") or 0.0
            )
            component_output_cost = float(
                component.get("output_cost_per_million") or 0.0
            )
            component_prompt_id = component.get("prompt_id")
            if component_prompt_id is not None:
                component_prompt_id = int(component_prompt_id)
            component_override_api_cost = component.get("override_api_cost")
            if component_override_api_cost is not None:
                component_override_api_cost = float(component_override_api_cost)
            component_idempotency_key = component.get("idempotency_key")
            if component_idempotency_key is not None:
                component_idempotency_key = str(
                    component_idempotency_key
                ).strip()
                if not component_idempotency_key:
                    component_idempotency_key = None
                elif len(component_idempotency_key) > 200:
                    raise ValueError("idempotency key is too long")
        except (AttributeError, TypeError, ValueError) as exc:
            raise BillingReservationError("AI usage component is invalid") from exc
        if min(component_input_cost, component_output_cost) < 0 or not all(
            math.isfinite(value)
            for value in (component_input_cost, component_output_cost)
        ):
            raise BillingReservationError("AI usage component rates are invalid")
        if component_override_api_cost is not None and (
            component_override_api_cost < 0
            or not math.isfinite(component_override_api_cost)
        ):
            raise BillingReservationError("AI usage component cost is invalid")
        normalized_component = {
            "input_tokens": component_input,
            "output_tokens": component_output,
            "input_cost_per_million": component_input_cost,
            "output_cost_per_million": component_output_cost,
            "prompt_id": component_prompt_id,
            "byok": bool(component.get("byok")),
        }
        if component_override_api_cost is not None:
            normalized_component["override_api_cost"] = (
                component_override_api_cost
            )
        if component_idempotency_key is not None:
            normalized_component["idempotency_key"] = component_idempotency_key

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                components_cursor = await conn.execute(
                    """
                    SELECT accumulated_components
                    FROM BILLING_USAGE_RESERVATIONS
                    WHERE id = ? AND user_id = ?
                      AND purpose = 'ai' AND status = 'active'
                    """,
                    (reservation_id, int(user_id)),
                )
                components_row = await components_cursor.fetchone()
                if components_row is None:
                    raise BillingReservationError(
                        "AI billing reservation is not active"
                    )
                try:
                    stored_components = orjson.loads(components_row[0] or "[]")
                    if not isinstance(stored_components, list):
                        stored_components = []
                except orjson.JSONDecodeError:
                    stored_components = []
                if normalized_component is not None:
                    idempotency_key = normalized_component.get(
                        "idempotency_key"
                    )
                    if idempotency_key is not None and any(
                        isinstance(stored, dict)
                        and stored.get("idempotency_key") == idempotency_key
                        for stored in stored_components
                    ):
                        await conn.rollback()
                        return
                    stored_components.append(normalized_component)
                cursor = await conn.execute(
                    """
                    UPDATE BILLING_USAGE_RESERVATIONS
                    SET accumulated_input_tokens =
                            COALESCE(accumulated_input_tokens, 0) + ?,
                        accumulated_output_tokens =
                            COALESCE(accumulated_output_tokens, 0) + ?,
                        accumulated_components = ?
                    WHERE id = ? AND user_id = ?
                      AND purpose = 'ai' AND status = 'active'
                    RETURNING id
                    """,
                    (
                        input_tokens,
                        output_tokens,
                        orjson.dumps(stored_components).decode(),
                        reservation_id,
                        int(user_id),
                    ),
                )
                if await cursor.fetchone() is None:
                    raise BillingReservationError(
                        "AI billing reservation is not active"
                    )
                await conn.commit()
                return
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not accumulate AI provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not accumulate AI provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not accumulate AI provider usage"
    ) from last_lock_error


async def mark_ai_reservation_provider_started(
    *,
    reservation_id: str | None,
    user_id: int,
) -> bool:
    """Durably preserve an active AI hold whose provider usage is uncertain.

    Realtime cancellation normally yields a final usage event.  If that event
    is absent, this marker prevents the request-finalizer from treating an
    already-started provider call as unused and refunding it immediately.
    """

    if not reservation_id:
        return False
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                cursor = await conn.execute(
                    """
                    UPDATE BILLING_USAGE_RESERVATIONS
                    SET provider_started_at=COALESCE(
                        provider_started_at,CURRENT_TIMESTAMP
                    )
                    WHERE id=? AND user_id=? AND purpose='ai'
                      AND status='active'
                    RETURNING id
                    """,
                    (reservation_id, int(user_id)),
                )
                marked = await cursor.fetchone() is not None
                await conn.commit()
                return marked
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not preserve uncertain AI provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not preserve uncertain AI provider usage"
                ) from exc
        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))
    raise BillingReservationError(
        "Could not preserve uncertain AI provider usage"
    ) from last_lock_error


async def ai_reservation_provider_started(
    *,
    reservation_id: str | None,
    user_id: int,
) -> bool:
    """Return whether an active AI hold crossed the provider boundary."""

    if not reservation_id:
        return False
    try:
        async with get_db_connection(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT provider_started_at IS NOT NULL
                FROM BILLING_USAGE_RESERVATIONS
                WHERE id=? AND user_id=? AND purpose='ai' AND status='active'
                """,
                (reservation_id, int(user_id)),
            )
            row = await cursor.fetchone()
        return bool(row and row[0])
    except Exception as exc:
        raise BillingReservationError(
            "Could not inspect uncertain AI provider usage"
        ) from exc


async def accumulate_ai_provider_call_usage(
    *,
    reservation_id: str | None,
    user_id: int,
    reported_input_tokens,
    reported_output_tokens,
    input_payload,
    output_payload,
    input_token_fallback=0,
    output_token_cap: int,
    llm_id: int | None = None,
    model: str | None = None,
    prompt_id: int | None = None,
    byok: bool = False,
) -> tuple[int, int]:
    """Normalize one successful provider call and append it to its hold."""
    try:
        input_tokens = max(0, int(reported_input_tokens or 0))
    except (TypeError, ValueError):
        input_tokens = 0
    try:
        output_tokens = max(0, int(reported_output_tokens or 0))
    except (TypeError, ValueError):
        output_tokens = 0
    try:
        extra_input_tokens = max(0, int(input_token_fallback or 0))
    except (TypeError, ValueError):
        extra_input_tokens = 0
    if input_tokens == 0:
        input_tokens = max(
            estimate_structured_usage_tokens(input_payload),
            extra_input_tokens,
        )
    if output_tokens == 0 and _structured_has_content(output_payload):
        output_tokens = min(
            max(1, int(output_token_cap or 1)),
            estimate_structured_usage_tokens(output_payload),
        )
    from common import get_llm_token_costs

    input_cost, output_cost = await get_llm_token_costs(
        model=model,
        llm_id=llm_id,
    )
    await accumulate_ai_reservation_usage(
        reservation_id=reservation_id,
        user_id=int(user_id),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        component={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "input_cost_per_million": input_cost,
            "output_cost_per_million": output_cost,
            "prompt_id": prompt_id,
            "byok": byok,
        },
    )
    return input_tokens, output_tokens


async def extend_ai_reservation(
    *,
    reservation_id: str | None,
    user_id: int,
    additional_amount: float,
) -> None:
    """Atomically add a second provider-call maximum to an active AI hold."""
    if not reservation_id:
        raise BillingReservationError("AI billing reservation is missing")
    additional_amount = float(additional_amount or 0.0)
    if not math.isfinite(additional_amount) or additional_amount < 0:
        raise BillingReservationError("AI reservation extension is invalid")
    if additional_amount == 0:
        return

    user_id = int(user_id)
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                reservation_cursor = await conn.execute(
                    """
                    SELECT billing_account_id
                    FROM BILLING_USAGE_RESERVATIONS
                    WHERE id = ? AND user_id = ?
                      AND purpose = 'ai' AND status = 'active'
                    """,
                    (reservation_id, user_id),
                )
                reservation = await reservation_cursor.fetchone()
                if reservation is None:
                    raise BillingReservationError(
                        "AI billing reservation is not active"
                    )
                payer_id = int(reservation[0])

                member_cursor = await conn.execute(
                    """
                    SELECT billing_account_id, billing_limit,
                           billing_limit_action, billing_current_month_spent,
                           billing_month_reset_date, billing_auto_refill_amount,
                           billing_max_limit
                    FROM USER_DETAILS
                    WHERE user_id = ?
                    """,
                    (user_id,),
                )
                member = await member_cursor.fetchone()
                if member is None or int(member[0] or user_id) != payer_id:
                    raise BillingReservationError(
                        "Billing account changed during AI usage"
                    )

                is_team_billed = member[0] is not None and payer_id != user_id
                billing_limit_delta = 0.0
                billing_refill_count_delta = 0
                if is_team_billed:
                    spent = float(member[3] or 0.0)
                    if str(member[4] or "") != current_month:
                        spent = 0.0
                        await conn.execute(
                            """
                            UPDATE USER_DETAILS
                            SET billing_current_month_spent = 0,
                                billing_month_reset_date = ?,
                                billing_auto_refill_count = 0
                            WHERE user_id = ?
                            """,
                            (current_month, user_id),
                        )
                    billing_limit = (
                        float(member[1]) if member[1] is not None else None
                    )
                    limit_action = str(member[2] or "block").lower()
                    requested_spend = spent + additional_amount
                    max_limit = (
                        float(member[6]) if member[6] is not None else None
                    )
                    if (
                        limit_action == "auto_refill"
                        and max_limit is not None
                        and requested_spend > max_limit + 1e-12
                    ):
                        raise BillingLimitExceededError(
                            "Maximum monthly billing limit reached"
                        )
                    if billing_limit is not None and requested_spend > billing_limit:
                        if limit_action == "auto_refill":
                            refill_amount = float(member[5] or 0.0)
                            (
                                new_limit,
                                billing_limit_delta,
                                billing_refill_count_delta,
                            ) = _calculate_auto_refill_adjustment(
                                current_limit=billing_limit,
                                requested_spend=requested_spend,
                                refill_amount=refill_amount,
                                max_limit=max_limit,
                            )
                            await conn.execute(
                                """
                                UPDATE USER_DETAILS
                                SET billing_limit = ?,
                                    billing_auto_refill_count =
                                        COALESCE(billing_auto_refill_count, 0) + ?
                                WHERE user_id = ?
                                """,
                                (new_limit, billing_refill_count_delta, user_id),
                            )
                        elif limit_action != "notify":
                            raise BillingLimitExceededError(
                                "Monthly billing limit reached"
                            )

                balance_cursor = await conn.execute(
                    """
                    UPDATE USER_DETAILS
                    SET balance = COALESCE(balance, 0) - ?
                    WHERE user_id = ? AND COALESCE(balance, 0) >= ?
                    RETURNING balance
                    """,
                    (additional_amount, payer_id, additional_amount),
                )
                if await balance_cursor.fetchone() is None:
                    raise InsufficientBalanceError("Insufficient balance")
                if is_team_billed:
                    await conn.execute(
                        """
                        UPDATE USER_DETAILS
                        SET billing_current_month_spent =
                            COALESCE(billing_current_month_spent, 0) + ?
                        WHERE user_id = ?
                        """,
                        (additional_amount, user_id),
                    )
                extension_cursor = await conn.execute(
                    """
                    UPDATE BILLING_USAGE_RESERVATIONS
                    SET amount = amount + ?,
                        billing_limit_delta =
                            COALESCE(billing_limit_delta, 0) + ?,
                        billing_refill_count_delta =
                            COALESCE(billing_refill_count_delta, 0) + ?
                    WHERE id = ? AND user_id = ?
                      AND purpose = 'ai' AND status = 'active'
                    """,
                    (
                        additional_amount,
                        billing_limit_delta,
                        billing_refill_count_delta,
                        reservation_id,
                        user_id,
                    ),
                )
                if extension_cursor.rowcount != 1:
                    raise BillingReservationError(
                        "AI billing reservation changed during extension"
                    )
                await conn.commit()
                return
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not extend AI provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not extend AI provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not extend AI provider usage"
    ) from last_lock_error


async def _release_team_reservation_state(
    conn,
    *,
    user_id: int,
    payer_id: int,
    amount: float,
    billing_month: str,
    billing_limit_delta: float,
    billing_refill_count_delta: int,
) -> None:
    """Undo the temporary team-spend and auto-refill effects of one hold."""
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    if user_id != payer_id and billing_month == current_month:
        await conn.execute(
            """
            UPDATE USER_DETAILS
            SET billing_current_month_spent = MAX(
                0,
                COALESCE(billing_current_month_spent, 0) - ?
            )
            WHERE user_id = ?
            """,
            (amount, user_id),
        )

    limit_delta = max(0.0, float(billing_limit_delta or 0.0))
    refill_count_delta = max(0, int(billing_refill_count_delta or 0))
    if limit_delta == 0 and refill_count_delta == 0:
        return

    member_cursor = await conn.execute(
        """
        SELECT billing_limit, billing_current_month_spent,
               billing_month_reset_date
        FROM USER_DETAILS
        WHERE user_id = ?
        """,
        (user_id,),
    )
    member = await member_cursor.fetchone()
    if member is None:
        return
    # Auto-refill counters are month-scoped. Once a new month has begun, an
    # older hold must not subtract limit/count increments created by newer
    # reservations that now share the same aggregate USER_DETAILS fields.
    if billing_month != current_month or str(member[2] or "") != current_month:
        return

    current_limit = float(member[0]) if member[0] is not None else None
    remaining_month_spend = max(0.0, float(member[1] or 0.0))
    new_limit = None
    if current_limit is not None:
        new_limit = max(remaining_month_spend, current_limit - limit_delta)

    await conn.execute(
        """
        UPDATE USER_DETAILS
        SET billing_limit = ?,
            billing_auto_refill_count = MAX(
                0,
                COALESCE(billing_auto_refill_count, 0) - ?
            )
        WHERE user_id = ?
        """,
        (new_limit, refill_count_delta, user_id),
    )


async def prepare_ai_reservation_settlement(
    conn,
    *,
    reservation_id: str | None,
    user_id: int,
) -> AiReservationCredit | None:
    """Return a held AI maximum inside the caller's billing transaction."""
    if not reservation_id:
        return None
    cursor = await conn.execute(
        """
        SELECT user_id, billing_account_id, amount, billing_month, status, purpose,
               accumulated_input_tokens, accumulated_output_tokens,
               billing_limit_delta, billing_refill_count_delta,
               accumulated_components
        FROM BILLING_USAGE_RESERVATIONS
        WHERE id = ?
        """,
        (reservation_id,),
    )
    row = await cursor.fetchone()
    if not row or row[4] != "active" or row[5] != "ai":
        raise BillingReservationError("AI billing reservation is not active")
    if int(row[0]) != int(user_id):
        raise BillingReservationError("AI billing reservation belongs to another user")

    payer_id = int(row[1])
    maximum_amount = float(row[2])
    accumulated_api_cost = _accumulated_components_api_cost(row[10])
    result = await conn.execute(
        """
        UPDATE USER_DETAILS
        SET balance = COALESCE(balance, 0) + ?
        WHERE user_id = ?
        RETURNING balance
        """,
        (maximum_amount, payer_id),
    )
    payer_row = await result.fetchone()
    if payer_row is None:
        raise BillingReservationError("AI billing account is unavailable")

    await _release_team_reservation_state(
        conn,
        user_id=int(row[0]),
        payer_id=payer_id,
        amount=maximum_amount,
        billing_month=str(row[3]),
        billing_limit_delta=float(row[8] or 0.0),
        billing_refill_count_delta=int(row[9] or 0),
    )

    return AiReservationCredit(
        reservation_id=reservation_id,
        user_id=int(row[0]),
        billing_account_id=payer_id,
        maximum_amount=maximum_amount,
        payer_balance_with_credit=float(payer_row[0]),
        accumulated_input_tokens=max(0, int(row[6] or 0)),
        accumulated_output_tokens=max(0, int(row[7] or 0)),
        accumulated_api_cost=accumulated_api_cost,
    )


async def complete_ai_reservation_settlement(
    conn,
    credit: AiReservationCredit | None,
) -> float:
    """Mark an AI hold settled after actual token billing in the same txn."""
    if credit is None:
        return 0.0
    cursor = await conn.execute(
        "SELECT balance FROM USER_DETAILS WHERE user_id = ?",
        (credit.billing_account_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise BillingReservationError("AI billing account is unavailable")
    actual_amount = max(0.0, credit.payer_balance_with_credit - float(row[0]))
    if actual_amount > credit.maximum_amount + 1e-9:
        raise BillingReservationError(
            "Actual AI charge exceeded its reserved maximum"
        )
    result = await conn.execute(
        """
        UPDATE BILLING_USAGE_RESERVATIONS
        SET status = 'settled', settled_amount = ?, settled_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'active' AND purpose = 'ai'
        """,
        (actual_amount, credit.reservation_id),
    )
    if result.rowcount != 1:
        raise BillingReservationError("AI billing reservation changed during settlement")
    return actual_amount


async def settle_accumulated_ai_reservation_usage(
    *,
    reservation_id: str | None,
    user_id: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
    prompt_id: int | None,
    byok: bool,
) -> bool:
    """Capture a completed pre-tool call when no final response is saved."""
    if not reservation_id:
        return False
    try:
        input_cost_per_million = float(input_cost_per_million or 0.0)
        output_cost_per_million = float(output_cost_per_million or 0.0)
    except (TypeError, ValueError) as exc:
        raise BillingReservationError("AI billing rates are invalid") from exc
    if min(input_cost_per_million, output_cost_per_million) < 0 or not all(
        math.isfinite(value)
        for value in (input_cost_per_million, output_cost_per_million)
    ):
        raise BillingReservationError("AI billing rates are invalid")

    from common import consume_token

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                usage_cursor = await conn.execute(
                    """
                    SELECT accumulated_input_tokens, accumulated_output_tokens,
                           status, purpose, accumulated_components
                    FROM BILLING_USAGE_RESERVATIONS
                    WHERE id = ? AND user_id = ?
                    """,
                    (reservation_id, int(user_id)),
                )
                usage = await usage_cursor.fetchone()
                if (
                    usage is None
                    or usage[2] != "active"
                    or usage[3] != "ai"
                ):
                    await conn.rollback()
                    return False
                input_tokens = max(0, int(usage[0] or 0))
                output_tokens = max(0, int(usage[1] or 0))
                accumulated_api_cost = _accumulated_components_api_cost(
                    usage[4]
                )
                if (
                    input_tokens == 0
                    and output_tokens == 0
                    and accumulated_api_cost is None
                ):
                    await conn.rollback()
                    return False

                credit = await prepare_ai_reservation_settlement(
                    conn,
                    reservation_id=reservation_id,
                    user_id=int(user_id),
                )
                billing_cursor = await conn.execute("SELECT 1")
                billing_ok = await consume_token(
                    int(user_id),
                    input_tokens,
                    output_tokens,
                    input_cost_per_million,
                    output_cost_per_million,
                    conn,
                    billing_cursor,
                    prompt_id=prompt_id,
                    byok=bool(byok),
                    override_api_cost=credit.accumulated_api_cost,
                    billing_account_id_override=credit.billing_account_id,
                )
                if not billing_ok:
                    raise BillingReservationError(
                        "Could not capture accumulated AI provider usage"
                    )
                await complete_ai_reservation_settlement(conn, credit)
                await conn.commit()
                return True
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not capture accumulated AI provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not capture accumulated AI provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not capture accumulated AI provider usage"
    ) from last_lock_error


async def settle_ai_reservation_components(
    *,
    reservation_id: str | None,
    user_id: int,
    prompt_id: int | None,
    components: list[dict],
) -> bool:
    """Capture completed heterogeneous provider calls without saving a message."""
    if not reservation_id or not components:
        return False
    normalized = []
    for component in components:
        try:
            input_tokens = max(0, int(component.get("input_tokens") or 0))
            output_tokens = max(0, int(component.get("output_tokens") or 0))
            input_cost = float(component.get("input_cost_per_million") or 0.0)
            output_cost = float(component.get("output_cost_per_million") or 0.0)
            override_api_cost = component.get("override_api_cost")
            if override_api_cost is not None:
                override_api_cost = float(override_api_cost)
        except (TypeError, ValueError) as exc:
            raise BillingReservationError("AI usage component is invalid") from exc
        if min(input_cost, output_cost) < 0 or not all(
            math.isfinite(value) for value in (input_cost, output_cost)
        ):
            raise BillingReservationError("AI usage component rates are invalid")
        if override_api_cost is not None and (
            override_api_cost < 0 or not math.isfinite(override_api_cost)
        ):
            raise BillingReservationError("AI usage component cost is invalid")
        normalized.append(
            {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_cost": input_cost,
                "output_cost": output_cost,
                "byok": bool(component.get("byok")),
                "override_api_cost": override_api_cost,
            }
        )

    from common import consume_token

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                status_cursor = await conn.execute(
                    """
                    SELECT status, purpose
                    FROM BILLING_USAGE_RESERVATIONS
                    WHERE id = ? AND user_id = ?
                    """,
                    (reservation_id, int(user_id)),
                )
                status_row = await status_cursor.fetchone()
                if (
                    status_row is None
                    or status_row[0] != "active"
                    or status_row[1] != "ai"
                ):
                    await conn.rollback()
                    return False
                credit = await prepare_ai_reservation_settlement(
                    conn,
                    reservation_id=reservation_id,
                    user_id=int(user_id),
                )
                billing_cursor = await conn.execute("SELECT 1")
                for component in normalized:
                    billing_ok = await consume_token(
                        int(user_id),
                        component["input_tokens"],
                        component["output_tokens"],
                        component["input_cost"],
                        component["output_cost"],
                        conn,
                        billing_cursor,
                        prompt_id=prompt_id,
                        byok=component["byok"],
                        override_api_cost=component["override_api_cost"],
                        billing_account_id_override=credit.billing_account_id,
                    )
                    if not billing_ok:
                        raise BillingReservationError(
                            "Could not capture AI provider component"
                        )
                await complete_ai_reservation_settlement(conn, credit)
                await conn.commit()
                return True
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not capture AI provider components"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not capture AI provider components"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not capture AI provider components"
    ) from last_lock_error


async def _restore_reservation_credit(conn, row) -> None:
    """Restore a reservation row already transitioned to ``refunded``."""
    user_id = int(row[0])
    payer_id = int(row[1])
    amount = float(row[2])
    billing_month = str(row[3])
    billing_limit_delta = float(row[4] or 0.0)
    billing_refill_count_delta = int(row[5] or 0)
    await conn.execute(
        """
        UPDATE USER_DETAILS
        SET balance = COALESCE(balance, 0) + ?
        WHERE user_id = ?
        """,
        (amount, payer_id),
    )
    await _release_team_reservation_state(
        conn,
        user_id=user_id,
        payer_id=payer_id,
        amount=amount,
        billing_month=billing_month,
        billing_limit_delta=billing_limit_delta,
        billing_refill_count_delta=billing_refill_count_delta,
    )


async def claim_fixed_usage_provider(
    reservation_id: str,
    *,
    purpose: str,
    user_id: int,
) -> bool:
    """Atomically grant one worker permission to call a fixed-cost provider."""
    normalized_purpose = str(purpose or "").strip().lower()
    if normalized_purpose not in _USAGE_TOTAL_COLUMNS:
        raise BillingReservationError(f"Unsupported billing purpose: {purpose}")
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        async with get_db_connection() as conn:
            try:
                await conn.execute("BEGIN IMMEDIATE")
                cursor = await conn.execute(
                    """
                    UPDATE BILLING_USAGE_RESERVATIONS
                    SET provider_started_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ? AND purpose = ?
                      AND status = 'active' AND provider_started_at IS NULL
                    RETURNING id
                    """,
                    (reservation_id, int(user_id), normalized_purpose),
                )
                claimed = await cursor.fetchone() is not None
                await conn.commit()
                return claimed
            except sqlite3.OperationalError as exc:
                await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                else:
                    raise BillingReservationError(
                        "Could not claim provider reservation"
                    ) from exc
            except Exception as exc:
                await conn.rollback()
                raise BillingReservationError(
                    "Could not claim provider reservation"
                ) from exc
        await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))
    raise BillingReservationError("Could not claim provider reservation") from last_lock_error


async def mark_fixed_usage_provider_succeeded(
    reservation_id: str,
    *,
    purpose: str | None = None,
    user_id: int | None = None,
) -> bool:
    """Durably record that a fixed-cost provider completed billable work."""
    normalized_purpose = None
    if purpose is not None:
        normalized_purpose = str(purpose or "").strip().lower()
        if normalized_purpose not in _USAGE_TOTAL_COLUMNS:
            raise BillingReservationError(
                f"Unsupported billing purpose: {purpose}"
            )

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                clauses = [
                    "id = ?",
                    "status = 'active'",
                    "purpose IN ('image', 'stt', 'tts', 'video', 'phone')",
                ]
                parameters: list[object] = [reservation_id]
                if normalized_purpose is not None:
                    clauses.append("purpose = ?")
                    parameters.append(normalized_purpose)
                if user_id is not None:
                    clauses.append("user_id = ?")
                    parameters.append(int(user_id))
                cursor = await conn.execute(
                    f"""
                    UPDATE BILLING_USAGE_RESERVATIONS
                    SET provider_succeeded_at = COALESCE(
                        provider_succeeded_at,
                        CURRENT_TIMESTAMP
                    )
                    WHERE {' AND '.join(clauses)}
                    RETURNING id
                    """,
                    tuple(parameters),
                )
                marked = await cursor.fetchone() is not None
                await conn.commit()
                return marked
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not mark provider usage as successful"
                    ) from exc
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not mark provider usage as successful"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not mark provider usage as successful"
    ) from last_lock_error


async def _reconcile_one_stale_reservation(reservation_id: str) -> bool:
    """Reconcile one stale row so a malformed row cannot block the rest."""
    from common import consume_token

    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                cursor = await conn.execute(
                    f"""
                    SELECT user_id, purpose, accumulated_components,
                           provider_succeeded_at
                    FROM BILLING_USAGE_RESERVATIONS
                    WHERE id = ? AND {_STALE_RESERVATION_PREDICATE}
                    """,
                    (reservation_id,),
                )
                row = await cursor.fetchone()
                if row is None:
                    await conn.rollback()
                    return False

                user_id = int(row[0])
                components = []
                if row[1] == "ai":
                    try:
                        parsed_components = orjson.loads(row[2] or "[]")
                        if isinstance(parsed_components, list):
                            components = parsed_components
                    except orjson.JSONDecodeError:
                        components = []

                if components:
                    credit = await prepare_ai_reservation_settlement(
                        conn,
                        reservation_id=reservation_id,
                        user_id=user_id,
                    )
                    billing_cursor = await conn.execute("SELECT 1")
                    for component in components:
                        try:
                            component_input = max(
                                0,
                                int(component.get("input_tokens") or 0),
                            )
                            component_output = max(
                                0,
                                int(component.get("output_tokens") or 0),
                            )
                            component_input_cost = float(
                                component.get("input_cost_per_million") or 0.0
                            )
                            component_output_cost = float(
                                component.get("output_cost_per_million") or 0.0
                            )
                            component_prompt_id = component.get("prompt_id")
                            if component_prompt_id is not None:
                                component_prompt_id = int(component_prompt_id)
                            component_override_api_cost = component.get(
                                "override_api_cost"
                            )
                            if component_override_api_cost is not None:
                                component_override_api_cost = float(
                                    component_override_api_cost
                                )
                        except (AttributeError, TypeError, ValueError) as exc:
                            raise BillingReservationError(
                                "Stored AI usage component is invalid"
                            ) from exc
                        if min(
                            component_input_cost,
                            component_output_cost,
                        ) < 0 or not all(
                            math.isfinite(value)
                            for value in (
                                component_input_cost,
                                component_output_cost,
                            )
                        ):
                            raise BillingReservationError(
                                "Stored AI usage component rates are invalid"
                            )
                        if component_override_api_cost is not None and (
                            component_override_api_cost < 0
                            or not math.isfinite(component_override_api_cost)
                        ):
                            raise BillingReservationError(
                                "Stored AI usage component cost is invalid"
                            )
                        billed = await consume_token(
                            user_id,
                            component_input,
                            component_output,
                            component_input_cost,
                            component_output_cost,
                            conn,
                            billing_cursor,
                            prompt_id=component_prompt_id,
                            byok=bool(component.get("byok")),
                            override_api_cost=component_override_api_cost,
                            billing_account_id_override=credit.billing_account_id,
                        )
                        if not billed:
                            raise BillingReservationError(
                                "Could not settle stale AI usage"
                            )
                    await complete_ai_reservation_settlement(conn, credit)
                elif row[1] != "ai" and row[3] is not None:
                    settled = await settle_fixed_usage_in_transaction(
                        conn,
                        reservation_id,
                        expected_user_id=user_id,
                    )
                    if not settled:
                        raise BillingReservationError(
                            "Could not settle successful stale provider usage"
                        )
                else:
                    refund_cursor = await conn.execute(
                        """
                        UPDATE BILLING_USAGE_RESERVATIONS
                        SET status = 'refunded', refunded_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status = 'active'
                        RETURNING user_id, billing_account_id, amount, billing_month,
                                  billing_limit_delta, billing_refill_count_delta
                        """,
                        (reservation_id,),
                    )
                    refund_row = await refund_cursor.fetchone()
                    if refund_row is None:
                        await conn.rollback()
                        return False
                    await _restore_reservation_credit(conn, refund_row)

                await conn.commit()
                return True
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not reconcile provider usage"
                    ) from exc
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not reconcile provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError(
        "Could not reconcile provider usage"
    ) from last_lock_error


async def reconcile_stale_usage_reservations() -> int:
    """Settle/refund stale holds independently so one bad row is isolated."""
    async with get_db_connection(readonly=True) as conn:
        cursor = await conn.execute(
            f"""
            SELECT id
            FROM BILLING_USAGE_RESERVATIONS
            WHERE {_STALE_RESERVATION_PREDICATE}
            """
        )
        reservation_ids = [str(row[0]) for row in await cursor.fetchall()]

    reconciled = 0
    for reservation_id in reservation_ids:
        try:
            if await _reconcile_one_stale_reservation(reservation_id):
                reconciled += 1
        except BillingReservationError:
            logger.exception(
                "Could not reconcile stale billing reservation %s",
                reservation_id,
            )
    return reconciled


async def _maybe_reconcile_stale_usage_reservations() -> None:
    global _last_stale_reconciliation
    now = time.monotonic()
    if now - _last_stale_reconciliation < 300:
        return
    # Duplicate attempts are harmless because reconciliation itself is one
    # idempotent database transaction. Avoid a module-level asyncio.Lock here:
    # Dramatiq creates a fresh event loop for every background job.
    _last_stale_reconciliation = now
    try:
        reconciled = await reconcile_stale_usage_reservations()
        if reconciled:
            logger.warning("Reconciled %d stale billing reservation(s)", reconciled)
    except Exception:
        logger.exception("Could not reconcile stale billing reservations")


async def settle_fixed_usage_in_transaction(
    conn,
    reservation_id: str,
    *,
    expected_user_id: int | None = None,
) -> bool:
    """Finalize fixed usage inside the caller's existing DB transaction."""
    cursor = await conn.execute(
        """
        SELECT user_id, purpose, service_id, usage_quantity, amount, status
        FROM BILLING_USAGE_RESERVATIONS
        WHERE id = ?
        """,
        (reservation_id,),
    )
    row = await cursor.fetchone()
    if not row:
        raise BillingReservationError("Unknown billing reservation")
    if expected_user_id is not None and int(row[0]) != int(expected_user_id):
        raise BillingReservationError(
            "Billing reservation belongs to another user"
        )
    if row[5] == "settled":
        return True
    if row[5] != "active":
        return False

    user_id = int(row[0])
    purpose = str(row[1])
    service_id = int(row[2])
    usage_quantity = float(row[3])
    amount = float(row[4])
    total_column = _USAGE_TOTAL_COLUMNS.get(purpose)
    if purpose not in _USAGE_TOTAL_COLUMNS:
        raise BillingReservationError(
            f"Unsupported reservation purpose: {purpose}"
        )

    status_cursor = await conn.execute(
        """
        UPDATE BILLING_USAGE_RESERVATIONS
        SET status = 'settled', settled_amount = amount,
            settled_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'active'
        """,
        (reservation_id,),
    )
    if status_cursor.rowcount != 1:
        raise BillingReservationError(
            "Reservation changed while it was being settled"
        )
    await conn.execute(
        """
        INSERT INTO SERVICE_USAGE (user_id, service_id, usage_quantity, cost)
        VALUES (?, ?, ?, ?)
        """,
        (user_id, service_id, usage_quantity, amount),
    )
    if total_column:
        await conn.execute(
            f"""
            UPDATE USER_DETAILS
            SET total_cost = COALESCE(total_cost, 0) + ?,
                {total_column} = COALESCE({total_column}, 0) + ?
            WHERE user_id = ?
            """,
            (amount, amount, user_id),
        )
    else:
        await conn.execute(
            """
            UPDATE USER_DETAILS
            SET total_cost = COALESCE(total_cost, 0) + ?
            WHERE user_id = ?
            """,
            (amount, user_id),
        )
    daily_ok = await record_daily_usage(
        user_id=user_id,
        usage_type=purpose,
        cost=amount,
        units=usage_quantity,
        conn=conn,
    )
    if not daily_ok:
        raise BillingReservationError("Could not record daily usage")
    return True


async def settle_fixed_usage_amount_in_transaction(
    conn,
    reservation_id: str,
    *,
    actual_amount: float,
    actual_usage_quantity: float,
    expected_user_id: int | None = None,
) -> bool:
    """Settle a fixed hold for a confirmed amount no greater than its maximum.

    Provider-metered telephone components reserve a bounded tranche before
    crossing the external I/O boundary, but the provider may later confirm a
    smaller final duration.  This primitive returns the unused balance inside
    the caller's transaction while keeping the reservation, service usage and
    member/payer accounting consistent.  A zero actual amount is deliberately
    handled by ``refund_fixed_usage`` instead of manufacturing zero-unit usage.
    """

    normalized_amount = float(actual_amount)
    normalized_quantity = float(actual_usage_quantity)
    if (
        not math.isfinite(normalized_amount)
        or normalized_amount <= 0
        or not math.isfinite(normalized_quantity)
        or normalized_quantity <= 0
    ):
        raise BillingReservationError(
            "Actual fixed usage must be positive and finite"
        )

    cursor = await conn.execute(
        """
        SELECT user_id,billing_account_id,purpose,service_id,usage_quantity,
               amount,status,billing_month,billing_limit_delta,
               billing_refill_count_delta
        FROM BILLING_USAGE_RESERVATIONS WHERE id=?
        """,
        (reservation_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        raise BillingReservationError("Unknown billing reservation")
    user_id = int(row[0])
    if expected_user_id is not None and user_id != int(expected_user_id):
        raise BillingReservationError(
            "Billing reservation belongs to another user"
        )
    if str(row[6]) == "settled":
        return True
    if str(row[6]) != "active":
        return False

    reserved_quantity = float(row[4])
    reserved_amount = float(row[5])
    if normalized_amount > reserved_amount + 1e-12:
        raise BillingReservationError(
            "Actual fixed usage exceeds its reserved maximum"
        )
    if normalized_quantity > reserved_quantity + 1e-12:
        raise BillingReservationError(
            "Actual usage quantity exceeds its reserved maximum"
        )
    purpose = str(row[2])
    total_column = _USAGE_TOTAL_COLUMNS.get(purpose)
    if purpose not in _USAGE_TOTAL_COLUMNS:
        raise BillingReservationError(
            f"Unsupported reservation purpose: {purpose}"
        )
    service_id = int(row[3])
    payer_id = int(row[1])
    credit = max(0.0, reserved_amount - normalized_amount)

    status_cursor = await conn.execute(
        """
        UPDATE BILLING_USAGE_RESERVATIONS
        SET status='settled',settled_amount=?,usage_quantity=?,
            settled_at=CURRENT_TIMESTAMP
        WHERE id=? AND status='active'
        """,
        (normalized_amount, normalized_quantity, reservation_id),
    )
    if status_cursor.rowcount != 1:
        raise BillingReservationError(
            "Reservation changed while it was being settled"
        )
    if credit > 0:
        await conn.execute(
            "UPDATE USER_DETAILS SET balance=COALESCE(balance,0)+? WHERE user_id=?",
            (credit, payer_id),
        )
        await _release_team_reservation_state(
            conn,
            user_id=user_id,
            payer_id=payer_id,
            amount=credit,
            billing_month=str(row[7] or ""),
            billing_limit_delta=float(row[8] or 0.0),
            billing_refill_count_delta=int(row[9] or 0),
        )

    await conn.execute(
        """
        INSERT INTO SERVICE_USAGE(user_id,service_id,usage_quantity,cost)
        VALUES(?,?,?,?)
        """,
        (user_id, service_id, normalized_quantity, normalized_amount),
    )
    if total_column:
        await conn.execute(
            f"""
            UPDATE USER_DETAILS
            SET total_cost=COALESCE(total_cost,0)+?,
                {total_column}=COALESCE({total_column},0)+?
            WHERE user_id=?
            """,
            (normalized_amount, normalized_amount, user_id),
        )
    else:
        await conn.execute(
            """
            UPDATE USER_DETAILS SET total_cost=COALESCE(total_cost,0)+?
            WHERE user_id=?
            """,
            (normalized_amount, user_id),
        )
    daily_ok = await record_daily_usage(
        user_id=user_id,
        usage_type=purpose,
        cost=normalized_amount,
        units=normalized_quantity,
        conn=conn,
    )
    if not daily_ok:
        raise BillingReservationError("Could not record daily usage")
    return True


async def settle_fixed_usage(reservation_id: str) -> bool:
    """Finalize one active reservation and its accounting in one transaction."""
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                settled = await settle_fixed_usage_in_transaction(
                    conn,
                    reservation_id,
                )
                await conn.commit()
                return settled
            except BillingReservationError:
                if transaction_started:
                    await conn.rollback()
                raise
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not settle provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not settle provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    raise BillingReservationError("Could not settle provider usage") from last_lock_error


async def refund_fixed_usage(reservation_id: str) -> bool:
    """Idempotently restore the balance for an active failed reservation."""
    last_lock_error = None
    for attempt in range(DB_MAX_RETRIES):
        retry_needed = False
        async with get_db_connection() as conn:
            transaction_started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                transaction_started = True
                refunded = await refund_fixed_usage_in_transaction(
                    conn,
                    reservation_id,
                )
                await conn.commit()
                return refunded
            except sqlite3.OperationalError as exc:
                if transaction_started:
                    await conn.rollback()
                if is_lock_error(exc) and attempt < DB_MAX_RETRIES - 1:
                    last_lock_error = exc
                    retry_needed = True
                else:
                    raise BillingReservationError(
                        "Could not refund provider usage"
                    ) from exc
            except Exception as exc:
                if transaction_started:
                    await conn.rollback()
                raise BillingReservationError(
                    "Could not refund provider usage"
                ) from exc

        if retry_needed:
            await asyncio.sleep(DB_RETRY_DELAY_BASE * (attempt + 1))

    logger.error("Could not refund reservation %s: %s", reservation_id, last_lock_error)
    raise BillingReservationError("Could not refund provider usage") from last_lock_error


async def refund_fixed_usage_in_transaction(
    conn,
    reservation_id: str,
    *,
    expected_user_id: int | None = None,
) -> bool:
    """Refund one unconfirmed fixed hold inside an existing transaction."""

    clauses = [
        "id=?",
        "status='active'",
        "provider_succeeded_at IS NULL",
    ]
    parameters: list[object] = [reservation_id]
    if expected_user_id is not None:
        clauses.append("user_id=?")
        parameters.append(int(expected_user_id))
    cursor = await conn.execute(
        f"""
        UPDATE BILLING_USAGE_RESERVATIONS
        SET status='refunded',refunded_at=CURRENT_TIMESTAMP
        WHERE {' AND '.join(clauses)}
        RETURNING user_id,billing_account_id,amount,billing_month,
                  billing_limit_delta,billing_refill_count_delta
        """,
        tuple(parameters),
    )
    row = await cursor.fetchone()
    if row is not None:
        await _restore_reservation_credit(conn, row)
        return True
    status_cursor = await conn.execute(
        "SELECT user_id,status FROM BILLING_USAGE_RESERVATIONS WHERE id=?",
        (reservation_id,),
    )
    status_row = await status_cursor.fetchone()
    if status_row is None:
        raise BillingReservationError("Unknown billing reservation")
    if expected_user_id is not None and int(status_row[0]) != int(expected_user_id):
        raise BillingReservationError(
            "Billing reservation belongs to another user"
        )
    return str(status_row[1]) == "refunded"
