"""Bound hosted search with the ordinary usage ledger and admitted payer.

Adapters supply cumulative provider usage for one HTTP request. A missing final
usage record after transport leaves a durable hold for reconciliation; it is not
evidence that the provider did no work. Prices are configured in SERVICES.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from billing import usage_reservations as usage
from integrations.applications.billing import current_application_operation, reservation_operation


@dataclass
class HostedSearchUsage:
    user_id: int
    max_uses: int
    unit_cost: float
    reservation_id: str | None
    count: int | None = None
    claimed: bool = False
    finished: bool = False
    application_operation: object | None = None

    async def claim(self) -> None:
        """Revalidate admission immediately before crossing the provider boundary."""
        if self.claimed or self.finished:
            raise usage.BillingReservationError("Hosted search request already claimed")
        if self.reservation_id is None and self.application_operation is not None:
            await usage.revalidate_application_operation(self.application_operation)
        if self.reservation_id is not None:
            try:
                async with usage.get_db_connection(readonly=True) as connection:
                    operation = await reservation_operation(connection, self.reservation_id)
                if operation is not None:
                    await usage.revalidate_application_operation(operation)
                if not await usage.claim_fixed_usage_provider(
                    self.reservation_id, purpose="web_search", user_id=self.user_id
                ):
                    raise usage.BillingReservationError("Hosted search reservation unavailable")
            except BaseException:
                # No transport has started. Revocation cannot strand a new hold.
                await usage.refund_fixed_usage(self.reservation_id)
                self.finished = True
                raise
        self.claimed = True

    def observe_usage(self, count: int) -> None:
        """Usage fields can repeat at message start/delta; do not double charge."""
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= self.max_uses:
            raise usage.BillingReservationError("Hosted search usage exceeds its configured bound")
        self.count = max(self.count or 0, count)

    async def finish(self, *, complete: bool = False, rejected: bool = False) -> bool:
        """Settle only a complete provider count or release a proven unused hold.

        Cancellation, transport errors and incomplete streams retain the active
        reservation. Settlement uses its durable payer even after revocation.
        """
        if self.finished:
            return True
        if self.reservation_id is None:
            self.finished = True  # BYOK is charged directly by that provider.
            return True
        if not self.claimed or rejected or (complete and self.count == 0):
            self.finished = await usage.refund_fixed_usage(self.reservation_id)
            return self.finished
        if not complete or self.count is None:
            return False
        async with usage.get_db_connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                self.finished = await usage.settle_fixed_usage_amount_in_transaction(
                    connection, self.reservation_id,
                    actual_amount=self.unit_cost * self.count,
                    actual_usage_quantity=self.count, expected_user_id=self.user_id,
                )
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        if not self.finished:
            raise usage.BillingReservationError("Hosted search settlement unavailable")
        return True


async def prepare_hosted_search(
    provider: str, user_id: int, *, byok: bool = False, max_uses: int | None = None,
) -> HostedSearchUsage:
    """Reserve the configured maximum for one provider request, before I/O.

    SERVICES name is ``hosted_web_search_<provider>`` with unit ``search``;
    cost_per_unit is the customer tariff. SYSTEM_CONFIG can bound request uses
    with ``hosted_web_search_<provider>_max_uses`` (default five, at most five).
    No fallback price or alternative payer is inferred from missing settings.
    """
    if not provider or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in provider):
        raise usage.BillingReservationError("Invalid hosted search provider")
    key = "hosted_web_search_" + provider
    async with usage.get_db_connection(readonly=True) as connection:
        cursor = await connection.execute("SELECT value FROM SYSTEM_CONFIG WHERE key=?", (key + "_max_uses",))
        configured = await cursor.fetchone()
        try:
            bound = int(configured[0]) if configured else 5
        except (TypeError, ValueError) as exc:
            raise usage.BillingReservationError("Hosted search request bound is not configured") from exc
        if not 1 <= bound <= 5:
            raise usage.BillingReservationError("Hosted search request bound must be between one and five")
        if max_uses is not None:
            if isinstance(max_uses, bool) or not isinstance(max_uses, int) or not 1 <= max_uses <= bound:
                raise usage.BillingReservationError("Hosted search request exceeds configured bound")
            bound = max_uses
        if byok:
            return HostedSearchUsage(int(user_id), bound, 0, None,
                                     application_operation=current_application_operation(user_id))
        cursor = await connection.execute(
            "SELECT id,cost_per_unit,unit FROM SERVICES WHERE name=?", (key,)
        )
        rows = await cursor.fetchall()
    try:
        rate = float(rows[0][1]) if len(rows) == 1 else 0
        valid = len(rows) == 1 and rows[0][2] == "search" and math.isfinite(rate) and rate > 0
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise usage.BillingReservationError("Hosted search tariff is not configured for " + provider)
    reservation = await usage.reserve_fixed_usage(
        user_id=int(user_id), purpose="web_search", amount=bound * rate,
        service_id=int(rows[0][0]), usage_quantity=bound,
    )
    return HostedSearchUsage(int(user_id), bound, rate, reservation)
