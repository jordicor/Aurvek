"""Fund native media before dispatch and settle authoritative provider usage."""

import asyncio

from billing import usage_reservations as ledger
from integrations.telephony.gemini_live_billing import calculate_api_cost


class LiveUsageMeter:
    def __init__(self, *, user_id, prompt_id, kind, byok=False):
        self.user_id, self.prompt_id = user_id, prompt_id
        self.kind, self.byok = kind, byok
        self.reservation = None
        self.api_cost = 0.0
        self.tokens = 0
        self.reserved = 0.0
        self.seconds = 0.0
        self.seen = set()
        self.started = False
        self._lock = asyncio.Lock()

    async def cover(self):
        async with self._lock:
            await self._cover()

    async def _cover(self):
        # Gemini's entire bounded 128k context at its highest input rate,
        # plus 2048 output tokens; GPT Live has at most 30s between checks.
        headroom = .5 if self.kind == "gemini_live" else .05
        amount = await ledger.estimate_customer_charge_from_api_cost(
            user_id=self.user_id, prompt_id=self.prompt_id,
            api_cost=0 if self.byok else self.api_cost + headroom,
            maximum_tokens=self.tokens + (133120 if self.kind == "gemini_live" else 0))
        if self.reservation is None:
            self.reservation = await ledger.reserve_ai_usage(user_id=self.user_id, maximum_amount=amount)
        elif amount > self.reserved:
            await ledger.extend_ai_reservation(reservation_id=self.reservation, user_id=self.user_id,
                                               additional_amount=amount - self.reserved)
        self.reserved = max(amount, self.reserved)

    async def provider_started(self):
        if self.reservation and not self.started:
            await ledger.mark_ai_reservation_provider_started(reservation_id=self.reservation, user_id=self.user_id)
        self.started = True

    async def observe(self, event):
        if self.kind == "gemini_live":
            usage, key = event["usage"], event["usage_id"]
            inputs, outputs = usage.input_tokens, usage.output_tokens
            cost = calculate_api_cost(usage)
        else:
            seconds = float(event["usage"]["seconds"])
            if seconds < self.seconds:
                raise ValueError("Live usage moved backwards")
            inputs = outputs = 0
            cost, key = (seconds - self.seconds) * .05 / 60, f"seconds:{seconds}"
            self.seconds = seconds
        if key in self.seen:
            return
        self.seen.add(key)
        self.api_cost += cost
        self.tokens += inputs + outputs
        if self.reservation:
            await ledger.accumulate_ai_reservation_usage(
                reservation_id=self.reservation, user_id=self.user_id,
                input_tokens=inputs, output_tokens=outputs,
                component={"input_tokens": inputs, "output_tokens": outputs,
                           "input_cost_per_million": 0, "output_cost_per_million": 0,
                           "prompt_id": self.prompt_id, "byok": self.byok,
                           "override_api_cost": 0 if self.byok else cost,
                           "idempotency_key": key})

    async def close(self, *, confirmed):
        if not self.reservation:
            return
        if not self.started or confirmed and not self.seen:
            await ledger.refund_fixed_usage(self.reservation)
        elif confirmed:
            await ledger.settle_accumulated_ai_reservation_usage(
                reservation_id=self.reservation, user_id=self.user_id,
                input_cost_per_million=0, output_cost_per_million=0,
                prompt_id=self.prompt_id, byok=self.byok)
        # An unconfirmed provider boundary remains reserved for reconciliation.
