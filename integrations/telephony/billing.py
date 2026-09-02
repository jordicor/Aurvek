"""Durable, provider-aware billing for Aurvek's native telephone channel.

Rates are administrator data.  This module deliberately contains no provider
prices, country assumptions or SERVICES identifiers.
"""

from __future__ import annotations

import asyncio
import math
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Mapping

import phonenumbers

from billing.usage_reservations import (
    BillingReservationError,
    InsufficientBalanceError,
    claim_fixed_usage_provider,
    refund_fixed_usage,
    refund_fixed_usage_in_transaction,
    reserve_fixed_usage,
    settle_fixed_usage_amount_in_transaction,
)
from database import get_db_connection


PHONE_COMPONENT_TYPES = frozenset(
    {"pstn", "transport", "stt", "tts", "amd", "recording"}
)
PHONE_COMPONENT_UNITS = {
    "pstn": "minute",
    "transport": "minute",
    "stt": "minute",
    "tts": "character",
    "amd": "call",
    "recording": "minute",
}
PHONE_TERMINAL_STATUSES = frozenset(
    {"completed", "busy", "no_answer", "machine", "failed", "canceled", "unresolved"}
)
DEFAULT_PSTN_TRANCHE_SECONDS = 60
DEFAULT_STREAM_TRANCHE_SECONDS = 15
DEFAULT_CLOSE_BUFFER_SECONDS = 5


class PhoneBillingError(RuntimeError):
    """A telephone charge could not be represented safely."""


class PhoneBillingConfigurationError(PhoneBillingError):
    """No unambiguous administrator-supplied rate matches the usage."""


class PhoneBillingExhausted(PhoneBillingError):
    """The canonical payer cannot cover the next external operation."""


class PhoneBillingAmbiguous(PhoneBillingError):
    """The provider may have charged but no safe settlement is known."""


@dataclass(frozen=True, slots=True)
class PhoneBillingRate:
    id: int
    provider: str
    component_type: str
    direction: str
    from_country: str
    to_country: str
    unit: str
    provider_rate_per_unit: float
    customer_rate_per_unit: float
    currency: str
    service_id: int | None


@dataclass(frozen=True, slots=True)
class PhoneCostComponent:
    id: int
    call_id: str
    reservation_id: str | None
    provider: str
    component_type: str
    dedupe_key: str
    quantity: float
    reserved_quantity: float
    unit: str
    state: str
    provider_cost: float
    customer_charge: float
    currency: str | None
    rate_missing: bool


ConnectionFactory = Callable[..., Any]


def country_for_e164(value: str) -> str:
    try:
        parsed = phonenumbers.parse(str(value), None)
    except phonenumbers.NumberParseException as exc:
        raise PhoneBillingConfigurationError(
            "Telephone billing country is unavailable"
        ) from exc
    country = str(phonenumbers.region_code_for_number(parsed) or "").upper()
    if len(country) != 2 or not country.isalpha():
        raise PhoneBillingConfigurationError(
            "Telephone billing country is unavailable"
        )
    return country


def _number_country(e164: str, stored_country: Any) -> str:
    """Resolve a line country from E.164 and reject contradictory inventory."""

    derived = country_for_e164(e164)
    stored = str(stored_country or "").strip().upper()
    if stored and (
        len(stored) != 2 or not stored.isalpha() or stored != derived
    ):
        raise PhoneBillingConfigurationError(
            "Telephone number country conflicts with its E.164 identity"
        )
    return derived


def _normalize_provider(value: str) -> str:
    provider = str(value or "").strip().lower()
    if not provider or len(provider) > 100:
        raise PhoneBillingConfigurationError("Telephone billing provider is invalid")
    return provider


def _normalize_component(value: str) -> str:
    component = str(value or "").strip().lower()
    if component not in PHONE_COMPONENT_TYPES:
        raise PhoneBillingConfigurationError("Telephone billing component is invalid")
    return component


def _positive_quantity(value: float) -> float:
    quantity = float(value)
    if not math.isfinite(quantity) or quantity <= 0:
        raise PhoneBillingError("Telephone billing quantity must be positive")
    return quantity


def _money(value: float) -> float:
    amount = float(value)
    if not math.isfinite(amount) or amount < 0:
        raise PhoneBillingConfigurationError("Telephone billing rate is invalid")
    return amount


def _row_component(row: Mapping[str, Any]) -> PhoneCostComponent:
    return PhoneCostComponent(
        id=int(row["id"]),
        call_id=str(row["call_id"]),
        reservation_id=(
            str(row["billing_reservation_id"])
            if row["billing_reservation_id"] is not None
            else None
        ),
        provider=str(row["provider"]),
        component_type=str(row["component_type"]),
        dedupe_key=str(row["dedupe_key"]),
        quantity=float(row["quantity"]),
        reserved_quantity=float(row["reserved_quantity"]),
        unit=str(row["unit"]),
        state=str(row["state"]),
        provider_cost=float(row["provider_cost"]),
        customer_charge=float(row["customer_charge"]),
        currency=(str(row["currency"]) if row["currency"] is not None else None),
        rate_missing=bool(row["rate_missing"]),
    )


async def _validated_rate_row(
    conn: Any,
    row: Mapping[str, Any],
    *,
    provider: str,
    component: str,
) -> PhoneBillingRate:
    expected_unit = PHONE_COMPONENT_UNITS[component]
    if str(row["unit"]) != expected_unit:
        raise PhoneBillingConfigurationError(
            f"{provider}/{component} rate must use {expected_unit}"
        )
    provider_rate = _money(row["provider_rate_per_unit"])
    customer_rate = _money(row["customer_rate_per_unit"])
    currency = str(row["currency"] or "").strip().upper()
    if not 3 <= len(currency) <= 8 or not currency.isalpha():
        raise PhoneBillingConfigurationError(
            f"{provider}/{component} currency is invalid"
        )
    service_id = int(row["service_id"]) if row["service_id"] is not None else None
    if customer_rate > 0 and service_id is None:
        raise PhoneBillingConfigurationError(
            f"{provider}/{component} customer rate has no SERVICES row"
        )
    if service_id is not None:
        cursor = await conn.execute("SELECT 1 FROM SERVICES WHERE id=?", (service_id,))
        if await cursor.fetchone() is None:
            raise PhoneBillingConfigurationError(
                f"{provider}/{component} SERVICES row is unavailable"
            )
    return PhoneBillingRate(
        id=int(row["id"]),
        provider=provider,
        component_type=component,
        direction=str(row["direction"]),
        from_country=str(row["from_country"]),
        to_country=str(row["to_country"]),
        unit=expected_unit,
        provider_rate_per_unit=provider_rate,
        customer_rate_per_unit=customer_rate,
        currency=currency,
        service_id=service_id,
    )


async def _load_call(conn: Any, call_id: str) -> dict[str, Any]:
    cursor = await conn.execute(
        """
        SELECT c.*,n.iso_country AS number_country
        FROM PHONE_CALLS c
        JOIN TELEPHONY_NUMBERS n ON n.id=c.telephony_number_id
        WHERE c.id=? AND c.deleted_at IS NULL
        """,
        (str(call_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise PhoneBillingError("Phone call is unavailable")
    return dict(row)


def _call_countries(call: Mapping[str, Any]) -> tuple[str, str]:
    number_e164 = str(
        call["from_e164"] if call["direction"] == "outbound" else call["to_e164"]
    )
    number_country = _number_country(number_e164, call.get("number_country"))
    if str(call["direction"]) == "outbound":
        return number_country, country_for_e164(str(call["to_e164"]))
    return country_for_e164(str(call["from_e164"])), number_country


async def resolve_phone_billing_rate(
    conn: Any,
    *,
    call: Mapping[str, Any],
    provider: str,
    component_type: str,
) -> PhoneBillingRate:
    provider = _normalize_provider(provider)
    component = _normalize_component(component_type)
    geographic = component == "pstn"
    from_country, to_country = _call_countries(call) if geographic else ("", "")
    direction = str(call["direction"]) if geographic else ""
    cursor = await conn.execute(
        """
        SELECT * FROM PHONE_BILLING_RATES
        WHERE active=1 AND provider=? AND component_type=?
          AND direction=? AND from_country=? AND to_country=?
        """,
        (provider, component, direction, from_country, to_country),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    if len(rows) != 1:
        raise PhoneBillingConfigurationError(
            f"No single exact {provider}/{component} telephone billing rate"
        )
    return await _validated_rate_row(
        conn,
        rows[0],
        provider=provider,
        component=component,
    )


async def upsert_phone_billing_rate(
    conn: Any,
    *,
    provider: str,
    component_type: str,
    direction: str = "",
    from_country: str = "",
    to_country: str = "",
    unit: str,
    provider_rate_per_unit: float,
    customer_rate_per_unit: float,
    currency: str,
    service_id: int | None,
    active: bool = True,
) -> int:
    """Validate and store one explicit administrator-supplied rate."""

    normalized_provider = _normalize_provider(provider)
    component = _normalize_component(component_type)
    normalized_direction = str(direction or "").strip().lower()
    if normalized_direction not in {"", "inbound", "outbound"}:
        raise PhoneBillingConfigurationError("Telephone rate direction is invalid")
    normalized_from = str(from_country or "").strip().upper()
    normalized_to = str(to_country or "").strip().upper()
    for country in (normalized_from, normalized_to):
        if country and (len(country) != 2 or not country.isalpha()):
            raise PhoneBillingConfigurationError("Telephone rate country is invalid")
    if component == "pstn" and (
        not normalized_direction or not normalized_from or not normalized_to
    ):
        raise PhoneBillingConfigurationError(
            "PSTN rates require exact direction, origin and destination"
        )
    if component != "pstn" and (
        normalized_direction or normalized_from or normalized_to
    ):
        raise PhoneBillingConfigurationError(
            "Only PSTN rates may have direction or country dimensions"
        )
    expected_unit = PHONE_COMPONENT_UNITS[component]
    if str(unit) != expected_unit:
        raise PhoneBillingConfigurationError(
            f"{component} rates must use {expected_unit}"
        )
    provider_rate = _money(provider_rate_per_unit)
    customer_rate = _money(customer_rate_per_unit)
    normalized_currency = str(currency or "").strip().upper()
    if not 3 <= len(normalized_currency) <= 8 or not normalized_currency.isalpha():
        raise PhoneBillingConfigurationError("Telephone billing currency is invalid")
    normalized_service = int(service_id) if service_id is not None else None
    if customer_rate > 0 and normalized_service is None:
        raise PhoneBillingConfigurationError(
            "A positive customer rate requires a SERVICES row"
        )
    if normalized_service is not None:
        cursor = await conn.execute(
            "SELECT 1 FROM SERVICES WHERE id=?", (normalized_service,)
        )
        if await cursor.fetchone() is None:
            raise PhoneBillingConfigurationError("Telephone SERVICES row is invalid")
    cursor = await conn.execute(
        """
        INSERT INTO PHONE_BILLING_RATES(
            provider,component_type,direction,from_country,to_country,unit,
            provider_rate_per_unit,customer_rate_per_unit,currency,service_id,active
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(provider,component_type,direction,from_country,to_country)
        DO UPDATE SET unit=excluded.unit,
          provider_rate_per_unit=excluded.provider_rate_per_unit,
          customer_rate_per_unit=excluded.customer_rate_per_unit,
          currency=excluded.currency,service_id=excluded.service_id,
          active=excluded.active,updated_at=CURRENT_TIMESTAMP
        RETURNING id
        """,
        (
            normalized_provider,component,normalized_direction,normalized_from,
            normalized_to,expected_unit,provider_rate,customer_rate,
            normalized_currency,normalized_service,int(bool(active)),
        ),
    )
    return int((await cursor.fetchone())[0])


async def phone_billing_readiness(
    conn: Any,
    *,
    config_overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return missing rate dimensions without inventing any monetary value."""

    requirements: set[tuple[str, str, str, str, str]] = {
        ("twilio", "transport", "", "", ""),
    }
    cursor = await conn.execute(
        "SELECT value FROM SYSTEM_CONFIG WHERE key='telephony_allowed_countries'"
    )
    row = await cursor.fetchone()
    override_values = dict(config_overrides or {})
    raw_allowed = override_values.get(
        "telephony_allowed_countries",
        row[0] if row is not None else "[]",
    )
    try:
        allowed = (
            raw_allowed
            if isinstance(raw_allowed, (list, tuple))
            else json.loads(str(raw_allowed))
        )
    except (TypeError, json.JSONDecodeError):
        allowed = []
    countries = {
        str(item).strip().upper()
        for item in allowed
        if len(str(item).strip()) == 2 and str(item).strip().isalpha()
    }
    country_errors: set[str] = set()
    cursor = await conn.execute(
        "SELECT DISTINCT e164,iso_country FROM TELEPHONY_NUMBERS WHERE enabled=1"
    )
    number_countries: set[str] = set()
    for number_e164, stored_country in await cursor.fetchall():
        try:
            number_countries.add(_number_country(str(number_e164), stored_country))
        except PhoneBillingConfigurationError:
            country_errors.add("twilio:pstn:number-country-unavailable")
    for origin in number_countries:
        for destination in countries:
            requirements.add(
                ("twilio", "pstn", "outbound", origin, destination)
            )
    cursor = await conn.execute(
        """
        SELECT DISTINCT c.e164,n.e164,n.iso_country
        FROM PHONE_CONVERSATION_BINDINGS b
        JOIN PHONE_CONTACTS c ON c.id=b.contact_id
        JOIN TELEPHONY_NUMBERS n ON n.id=COALESCE(
          b.preferred_number_id,
          (SELECT id FROM TELEPHONY_NUMBERS
           WHERE enabled=1 AND is_outbound_default=1 LIMIT 1)
        )
        WHERE b.active=1 AND b.allow_inbound=1 AND n.enabled=1
          AND n.inbound_enabled=1
        """
    )
    for contact_e164, number_e164, stored_number_country in await cursor.fetchall():
        try:
            caller_country = country_for_e164(str(contact_e164))
            destination = _number_country(
                str(number_e164), stored_number_country
            )
        except PhoneBillingConfigurationError:
            country_errors.add("twilio:pstn:number-country-unavailable")
            continue
        requirements.add(
            ("twilio", "pstn", "inbound", caller_country, destination)
        )
    cursor = await conn.execute(
        """
        SELECT DISTINCT lower(s.name)
        FROM VOICES v JOIN SERVICES s ON s.id=v.tts_service
        WHERE COALESCE(v.deprecated,0)=0 AND (
          COALESCE(v.is_default,0)=1 OR v.id IN (
            SELECT p.voice_id FROM PROMPTS p
            WHERE p.voice_id IS NOT NULL
          )
        )
        """
    )
    for service_name, in await cursor.fetchall():
        normalized = str(service_name or "")
        if "eleven" in normalized:
            requirements.add(("elevenlabs", "tts", "", "", ""))
        elif "openai" in normalized:
            requirements.add(("openai", "tts", "", "", ""))
    cursor = await conn.execute(
        """
        SELECT MAX(CASE WHEN amd_default=1 THEN 1 ELSE 0 END)
        FROM PROMPT_PHONE_SETTINGS
        """
    )
    feature_row = await cursor.fetchone()
    cursor = await conn.execute(
        """
        SELECT key,value FROM SYSTEM_CONFIG
        WHERE key='telephony_amd_default'
        """
    )
    global_features = {
        str(key): str(value or "").strip().lower() in {"1", "true", "yes", "on"}
        for key, value in await cursor.fetchall()
    }
    for key in ("telephony_amd_default",):
        if key in override_values:
            global_features[key] = str(override_values[key]).strip().lower() in {
                "1", "true", "yes", "on",
            }
    if (
        feature_row and int(feature_row[0] or 0)
    ) or global_features.get("telephony_amd_default", False):
        requirements.add(("twilio", "amd", "", "", ""))

    missing: set[str] = set(country_errors)
    for provider, component, direction, origin, destination in sorted(requirements):
        try:
            await _resolve_rate_dimensions(
                conn,
                provider=provider,
                component_type=component,
                direction=direction,
                from_country=origin,
                to_country=destination,
            )
        except PhoneBillingConfigurationError:
            label = f"{provider}:{component}"
            if direction or origin or destination:
                label += f":{direction or '*'}:{origin or '*'}:{destination or '*'}"
            missing.add(label)
    missing_rates = sorted(missing)
    return {
        "ready": not missing_rates,
        "missing_rates": missing_rates,
        "requirements": len(requirements) + len(country_errors),
    }


async def _resolve_rate_dimensions(
    conn: Any,
    *,
    provider: str,
    component_type: str,
    direction: str,
    from_country: str,
    to_country: str,
) -> PhoneBillingRate:
    # Readiness checks explicit dimensions directly; no synthetic telephone
    # numbers or wildcard fallback may turn a missing combination into ready.
    cursor = await conn.execute(
        """
        SELECT * FROM PHONE_BILLING_RATES
        WHERE active=1 AND provider=? AND component_type=?
          AND direction=? AND from_country=? AND to_country=?
        """,
        (provider, component_type, direction, from_country, to_country),
    )
    rows = [dict(row) for row in await cursor.fetchall()]
    if len(rows) != 1:
        raise PhoneBillingConfigurationError("Telephone billing rate is missing")
    return await _validated_rate_row(
        conn,
        provider=provider,
        component=component_type,
        row=rows[0],
    )


class PhoneBillingService:
    """Reserve, correlate and settle provider work for one phone call."""

    def __init__(
        self,
        *,
        connection_factory: ConnectionFactory = get_db_connection,
    ) -> None:
        self._connection_factory = connection_factory

    async def rate_available(
        self,
        *,
        call: Mapping[str, Any],
        provider: str,
        component_type: str,
    ) -> bool:
        """Check one call-specific provider rate without reserving usage."""

        try:
            async with self._connection_factory(readonly=True) as conn:
                await resolve_phone_billing_rate(
                    conn,
                    call=call,
                    provider=provider,
                    component_type=component_type,
                )
        except (PhoneBillingConfigurationError, PhoneBillingError):
            return False
        return True

    async def reserve_component(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        quantity: float,
        dedupe_key: str,
        occurred_at: str | None = None,
    ) -> PhoneCostComponent:
        component, _ = await self._reserve_component_owned(
            call_id=call_id,
            provider=provider,
            component_type=component_type,
            quantity=quantity,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at,
        )
        return component

    async def _reserve_component_owned(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        quantity: float,
        dedupe_key: str,
        occurred_at: str | None = None,
    ) -> tuple[PhoneCostComponent, bool]:
        """Return the component and whether this invocation created it."""

        normalized_quantity = _positive_quantity(quantity)
        key = str(dedupe_key or "").strip()
        if not key or len(key) > 500:
            raise PhoneBillingError("Telephone billing dedupe key is invalid")
        async with self._connection_factory(readonly=True) as conn:
            call = await _load_call(conn, call_id)
            if str(call["status"]) in PHONE_TERMINAL_STATUSES:
                raise PhoneBillingError("Terminal phone calls cannot reserve usage")
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE call_id=? AND dedupe_key=?",
                (str(call_id), key),
            )
            existing = await cursor.fetchone()
            if existing is not None:
                return _row_component(dict(existing)), False
            rate = await resolve_phone_billing_rate(
                conn,
                call=call,
                provider=provider,
                component_type=component_type,
            )
            owner_user_id = int(call["owner_user_id"])
            expected_currency = await self._existing_currency(conn, str(call_id))
        if expected_currency is not None and expected_currency != rate.currency:
            raise PhoneBillingConfigurationError(
                "A phone call cannot mix billing currencies"
            )

        estimated_provider = rate.provider_rate_per_unit * normalized_quantity
        estimated_customer = rate.customer_rate_per_unit * normalized_quantity
        reservation_id: str | None = None
        if estimated_customer > 0:
            assert rate.service_id is not None
            try:
                reservation_id = await reserve_fixed_usage(
                    user_id=owner_user_id,
                    purpose="phone",
                    amount=estimated_customer,
                    service_id=rate.service_id,
                    usage_quantity=normalized_quantity,
                )
            except InsufficientBalanceError as exc:
                raise PhoneBillingExhausted("Insufficient telephone balance") from exc
            except BillingReservationError as exc:
                raise PhoneBillingError("Telephone usage could not be reserved") from exc

        try:
            async with self._connection_factory() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                try:
                    call = await _load_call(conn, call_id)
                    if str(call["status"]) in PHONE_TERMINAL_STATUSES:
                        raise PhoneBillingError(
                            "Terminal phone calls cannot reserve usage"
                        )
                    cursor = await conn.execute(
                        """
                        INSERT INTO PHONE_CALL_COST_COMPONENTS(
                            call_id,billing_reservation_id,rate_id,provider,
                            component_type,dedupe_key,quantity,reserved_quantity,unit,
                            provider_rate_per_unit,customer_rate_per_unit,
                            estimated_provider_cost,estimated_customer_charge,
                            final_provider_cost,final_customer_charge,
                            provider_cost,customer_charge,currency,state,occurred_at,
                            provider_confirmed_at,settled_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(call_id,dedupe_key) DO NOTHING
                        RETURNING *
                        """,
                        (
                            str(call_id),reservation_id,rate.id,rate.provider,
                            rate.component_type,key,normalized_quantity,
                            normalized_quantity,rate.unit,
                            rate.provider_rate_per_unit,rate.customer_rate_per_unit,
                            estimated_provider,estimated_customer,
                            None,None,
                            estimated_provider,estimated_customer,rate.currency,
                            "reserved",
                            occurred_at,
                            None,None,
                        ),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        cursor = await conn.execute(
                            "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE call_id=? AND dedupe_key=?",
                            (str(call_id), key),
                        )
                        row = await cursor.fetchone()
                        if row is None:
                            raise PhoneBillingError(
                                "Telephone component dedupe lost its authoritative row"
                            )
                        inserted_own_reservation = False
                    else:
                        inserted_own_reservation = True
                    if inserted_own_reservation:
                        await self._refresh_call_totals(conn, str(call_id))
                    await conn.commit()
                    component = _row_component(dict(row))
                except BaseException:
                    await conn.rollback()
                    raise
        except BaseException:
            if reservation_id is not None:
                await refund_fixed_usage(reservation_id)
            raise
        if reservation_id is not None and not inserted_own_reservation:
            await refund_fixed_usage(reservation_id)
        return component, inserted_own_reservation

    async def record_cache_hit(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        quantity: float,
        dedupe_key: str,
    ) -> PhoneCostComponent:
        """Record provider-free reuse without charging the caller again."""

        normalized_quantity = _positive_quantity(quantity)
        key = str(dedupe_key or "").strip()
        if not key or len(key) > 500:
            raise PhoneBillingError("Telephone billing dedupe key is invalid")
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                call = await _load_call(conn, str(call_id))
                rate = await resolve_phone_billing_rate(
                    conn,
                    call=call,
                    provider=provider,
                    component_type=component_type,
                )
                cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_CALL_COST_COMPONENTS(
                        call_id,rate_id,provider,component_type,dedupe_key,
                        quantity,reserved_quantity,unit,provider_rate_per_unit,
                        customer_rate_per_unit,estimated_provider_cost,
                        estimated_customer_charge,final_provider_cost,
                        final_customer_charge,provider_cost,customer_charge,
                        currency,state,provider_confirmed_at,settled_at,last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,0,0,0,0,0,0,?,'settled',
                             CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,'provider_cache_hit')
                    ON CONFLICT(call_id,dedupe_key) DO NOTHING
                    RETURNING *
                    """,
                    (
                        str(call_id),rate.id,rate.provider,rate.component_type,key,
                        normalized_quantity,0,rate.unit,
                        rate.provider_rate_per_unit,rate.customer_rate_per_unit,
                        rate.currency,
                    ),
                )
                row = await cursor.fetchone()
                if row is None:
                    cursor = await conn.execute(
                        "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE call_id=? AND dedupe_key=?",
                        (str(call_id), key),
                    )
                    row = await cursor.fetchone()
                await self._refresh_call_totals(conn, str(call_id))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        if row is None:
            raise PhoneBillingError("Telephone cache usage was not recorded")
        return _row_component(dict(row))

    async def mark_provider_started(self, component_id: int) -> PhoneCostComponent:
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                (int(component_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            raise PhoneBillingError("Telephone cost component is unavailable")
        values = dict(row)
        component = _row_component(values)
        if component.state in {"provider_started", "settled"}:
            return component
        if component.state == "needs_attention":
            async with self._connection_factory() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                if component.reservation_id is not None:
                    cursor = await conn.execute(
                        """
                        SELECT status,provider_started_at
                        FROM BILLING_USAGE_RESERVATIONS WHERE id=?
                        """,
                        (component.reservation_id,),
                    )
                    reservation = await cursor.fetchone()
                    if (
                        reservation is None
                        or str(reservation[0]) != "active"
                        or reservation[1] is None
                    ):
                        await conn.rollback()
                        raise PhoneBillingError(
                            "Ambiguous telephone reservation cannot be reconciled"
                        )
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET state='provider_started',last_error=NULL,
                        provider_started_at=COALESCE(
                            provider_started_at,CURRENT_TIMESTAMP
                        ),updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (component.id,),
                )
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (component.id,),
                )
                reconciled = await cursor.fetchone()
                await conn.commit()
            return _row_component(dict(reconciled))
        if component.state != "reserved":
            raise PhoneBillingError("Telephone component cannot cross provider boundary")
        if component.reservation_id is None:
            async with self._connection_factory() as conn:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET state='provider_started',provider_started_at=COALESCE(
                        provider_started_at,CURRENT_TIMESTAMP
                    ),updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state='reserved'
                    """,
                    (component.id,),
                )
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (component.id,),
                )
                updated = await cursor.fetchone()
                await conn.commit()
            return _row_component(dict(updated))
        try:
            claimed = await claim_fixed_usage_provider(
                component.reservation_id,
                purpose="phone",
                user_id=await self._component_owner(component.id),
            )
        except BillingReservationError as exc:
            raise PhoneBillingError("Telephone provider reservation could not be claimed") from exc
        if not claimed:
            async with self._connection_factory(readonly=True) as conn:
                cursor = await conn.execute(
                    "SELECT provider_started_at,status FROM BILLING_USAGE_RESERVATIONS WHERE id=?",
                    (component.reservation_id,),
                )
                reservation = await cursor.fetchone()
            if reservation is None or reservation[0] is None or str(reservation[1]) != "active":
                raise PhoneBillingError("Telephone provider reservation is no longer active")
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            await conn.execute(
                """
                UPDATE PHONE_CALL_COST_COMPONENTS
                SET state='provider_started',provider_started_at=COALESCE(
                    provider_started_at,CURRENT_TIMESTAMP
                ),updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state='reserved'
                """,
                (component.id,),
            )
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                (component.id,),
            )
            updated = await cursor.fetchone()
            await conn.commit()
        if updated is None:
            raise PhoneBillingError("Telephone cost component disappeared")
        return _row_component(dict(updated))

    async def claim_provider_start(
        self,
        component_id: int,
    ) -> PhoneCostComponent | None:
        """Grant exactly one caller permission to cross this provider boundary."""

        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                (int(component_id),),
            )
            row = await cursor.fetchone()
        if row is None:
            raise PhoneBillingError("Telephone cost component is unavailable")
        component = _row_component(dict(row))
        if component.state != "reserved":
            return None
        if component.reservation_id is not None:
            try:
                claimed = await claim_fixed_usage_provider(
                    component.reservation_id,
                    purpose="phone",
                    user_id=await self._component_owner(component.id),
                )
            except BillingReservationError as exc:
                raise PhoneBillingError(
                    "Telephone provider reservation could not be claimed"
                ) from exc
            if not claimed:
                return None
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET state='provider_started',
                        provider_started_at=COALESCE(
                          provider_started_at,CURRENT_TIMESTAMP
                        ),updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state='reserved'
                    RETURNING *
                    """,
                    (component.id,),
                )
                claimed_row = await cursor.fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return _row_component(dict(claimed_row)) if claimed_row is not None else None

    async def settle_component(
        self,
        component_id: int,
        *,
        actual_quantity: float | None = None,
        external_usage_id: str | None = None,
    ) -> PhoneCostComponent:
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (int(component_id),),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise PhoneBillingError("Telephone cost component is unavailable")
                values = dict(row)
                component = _row_component(values)
                if component.state == "settled":
                    await conn.commit()
                    return component
                if component.state != "provider_started":
                    raise PhoneBillingError("Telephone component cannot be settled")
                quantity = (
                    component.reserved_quantity
                    if actual_quantity is None
                    else _positive_quantity(actual_quantity)
                )
                if quantity > component.reserved_quantity + 1e-12:
                    raise PhoneBillingError(
                        "Confirmed telephone usage exceeds its reservation"
                    )
                provider_cost = float(values["provider_rate_per_unit"]) * quantity
                customer_charge = float(values["customer_rate_per_unit"]) * quantity
                if component.reservation_id is not None:
                    await conn.execute(
                        """
                        UPDATE BILLING_USAGE_RESERVATIONS
                        SET provider_succeeded_at=COALESCE(
                            provider_succeeded_at,CURRENT_TIMESTAMP
                        )
                        WHERE id=? AND status='active'
                        """,
                        (component.reservation_id,),
                    )
                    await settle_fixed_usage_amount_in_transaction(
                        conn,
                        component.reservation_id,
                        actual_amount=customer_charge,
                        actual_usage_quantity=quantity,
                        expected_user_id=await self._component_owner(
                            component.id, conn=conn
                        ),
                    )
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET quantity=?,provider_cost=?,customer_charge=?,
                        final_provider_cost=?,final_customer_charge=?,
                        external_usage_id=COALESCE(?,external_usage_id),
                        state='settled',provider_confirmed_at=CURRENT_TIMESTAMP,
                        settled_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        quantity,provider_cost,customer_charge,provider_cost,
                        customer_charge,external_usage_id,component.id,
                    ),
                )
                await self._refresh_call_totals(conn, component.call_id)
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (component.id,),
                )
                updated = await cursor.fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return _row_component(dict(updated))

    async def refund_component(
        self,
        component_id: int,
        *,
        reason: str,
    ) -> PhoneCostComponent:
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (int(component_id),),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise PhoneBillingError("Telephone cost component is unavailable")
                component = _row_component(dict(row))
                if component.state == "refunded":
                    await conn.commit()
                    return component
                if component.state == "settled":
                    raise PhoneBillingError("Settled telephone usage cannot be refunded")
                if component.reservation_id is not None:
                    refunded = await refund_fixed_usage_in_transaction(
                        conn,
                        component.reservation_id,
                        expected_user_id=await self._component_owner(
                            component.id, conn=conn
                        ),
                    )
                    if not refunded:
                        raise PhoneBillingAmbiguous(
                            "Telephone provider work was already confirmed"
                        )
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET quantity=0,provider_cost=0,customer_charge=0,
                        final_provider_cost=0,final_customer_charge=0,
                        state='refunded',refunded_at=CURRENT_TIMESTAMP,
                        last_error=?,updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """,
                    (str(reason)[:500], component.id),
                )
                await self._refresh_call_totals(conn, component.call_id)
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (component.id,),
                )
                updated = await cursor.fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return _row_component(dict(updated))

    async def refund_component_if_unstarted(
        self,
        component_id: int,
        *,
        reason: str,
    ) -> PhoneCostComponent:
        """Refund only while the durable provider boundary is still uncrossed."""

        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    """
                    SELECT x.*,r.provider_started_at AS reservation_started_at
                    FROM PHONE_CALL_COST_COMPONENTS x
                    LEFT JOIN BILLING_USAGE_RESERVATIONS r
                      ON r.id=x.billing_reservation_id
                    WHERE x.id=?
                    """,
                    (int(component_id),),
                )
                row = await cursor.fetchone()
                if row is None:
                    raise PhoneBillingError(
                        "Telephone cost component is unavailable"
                    )
                values = dict(row)
                component = _row_component(values)
                if (
                    component.state != "reserved"
                    or values.get("reservation_started_at") is not None
                ):
                    await conn.commit()
                    return component
                if component.reservation_id is not None:
                    refunded = await refund_fixed_usage_in_transaction(
                        conn,
                        component.reservation_id,
                        expected_user_id=await self._component_owner(
                            component.id, conn=conn
                        ),
                    )
                    if not refunded:
                        raise PhoneBillingAmbiguous(
                            "Telephone provider work was already confirmed"
                        )
                await conn.execute(
                    """
                    UPDATE PHONE_CALL_COST_COMPONENTS
                    SET quantity=0,provider_cost=0,customer_charge=0,
                        final_provider_cost=0,final_customer_charge=0,
                        state='refunded',refunded_at=CURRENT_TIMESTAMP,
                        last_error=?,updated_at=CURRENT_TIMESTAMP
                    WHERE id=? AND state='reserved'
                    """,
                    (str(reason)[:500], component.id),
                )
                await self._refresh_call_totals(conn, component.call_id)
                cursor = await conn.execute(
                    "SELECT * FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                    (component.id,),
                )
                updated = await cursor.fetchone()
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return _row_component(dict(updated))

    async def mark_ambiguous(self, component_id: int, *, reason: str) -> None:
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            cursor = await conn.execute(
                "SELECT call_id FROM PHONE_CALL_COST_COMPONENTS WHERE id=?",
                (int(component_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                await conn.rollback()
                raise PhoneBillingError("Telephone cost component is unavailable")
            await conn.execute(
                """
                UPDATE PHONE_CALL_COST_COMPONENTS
                SET state='needs_attention',last_error=?,updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND state IN ('reserved','provider_started')
                """,
                (str(reason)[:500], int(component_id)),
            )
            await self._refresh_call_totals(conn, str(row[0]))
            await conn.commit()

    async def reserve_time_tranche(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        tranche_index: int,
        seconds: int,
        stream_attempt: int | None = None,
    ) -> PhoneCostComponent:
        component, _ = await self._reserve_time_tranche_owned(
            call_id=call_id,
            provider=provider,
            component_type=component_type,
            tranche_index=tranche_index,
            seconds=seconds,
            stream_attempt=stream_attempt,
        )
        return component

    async def _reserve_time_tranche_owned(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        tranche_index: int,
        seconds: int,
        stream_attempt: int | None = None,
    ) -> tuple[PhoneCostComponent, bool]:
        if int(seconds) <= 0:
            raise PhoneBillingError("Telephone billing tranche is invalid")
        normalized_component = _normalize_component(component_type)
        if normalized_component in {"transport", "stt"}:
            if stream_attempt is None or int(stream_attempt) < 0:
                raise PhoneBillingError(
                    "Stream-scoped billing requires a stream attempt"
                )
            scope = f"attempt:{int(stream_attempt)}"
        else:
            if stream_attempt is not None:
                raise PhoneBillingError(
                    "Call-scoped billing cannot use a stream attempt"
                )
            scope = "call"
        return await self._reserve_component_owned(
            call_id=call_id,
            provider=provider,
            component_type=normalized_component,
            quantity=int(seconds) / 60.0,
            dedupe_key=(
                f"{provider}:{normalized_component}:{scope}:"
                f"tranche:{int(tranche_index)}"
            ),
        )

    async def settle_duration_components(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        duration_seconds: float,
        external_usage_id: str | None = None,
        stream_attempt: int | None = None,
    ) -> dict[str, float | int]:
        """Allocate one trusted final duration across its reserved tranches."""

        seconds = float(duration_seconds)
        if not math.isfinite(seconds) or seconds < 0 or seconds > 86_400:
            raise PhoneBillingError("Confirmed telephone duration is invalid")
        normalized_provider = _normalize_provider(provider)
        normalized_component = _normalize_component(component_type)
        if PHONE_COMPONENT_UNITS[normalized_component] != "minute":
            raise PhoneBillingError("Telephone component is not duration-metered")
        if normalized_component in {"transport", "stt"}:
            if stream_attempt is None or int(stream_attempt) < 0:
                raise PhoneBillingError(
                    "Stream duration requires a stream attempt"
                )
            scope = f"attempt:{int(stream_attempt)}"
        else:
            if stream_attempt is not None:
                raise PhoneBillingError(
                    "Call duration cannot use a stream attempt"
                )
            scope = "call"
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALL_COST_COMPONENTS
                WHERE call_id=? AND provider=? AND component_type=?
                  AND dedupe_key LIKE ?
                ORDER BY id
                """,
                (
                    str(call_id),normalized_provider,normalized_component,
                    f"{normalized_provider}:{normalized_component}:{scope}:tranche:%",
                ),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
        remaining = seconds / 60.0
        settled = 0
        refunded = 0
        for row in rows:
            component = _row_component(row)
            if component.state == "settled":
                remaining = max(0.0, remaining - component.quantity)
                settled += 1
                continue
            if component.state == "refunded":
                continue
            used = min(component.reserved_quantity, remaining)
            if used > 1e-12:
                if component.state in {"reserved", "needs_attention"}:
                    component = await self.mark_provider_started(component.id)
                await self.settle_component(
                    component.id,
                    actual_quantity=used,
                    external_usage_id=external_usage_id,
                )
                remaining = max(0.0, remaining - used)
                settled += 1
            else:
                await self.refund_component(
                    component.id,
                    reason="provider_final_duration_unused_tranche",
                )
                refunded += 1
        absorbed = 0.0
        if remaining > 1e-12:
            component, inserted = await self._record_confirmed_unreserved_usage(
                call_id=str(call_id),
                provider=normalized_provider,
                component_type=normalized_component,
                quantity=remaining,
                external_usage_id=external_usage_id,
                dedupe_key=(
                    f"{normalized_provider}:{normalized_component}:"
                    f"{scope}:unreserved-final:{external_usage_id or seconds}"
                ),
                reason="provider_final_exceeded_reserved_coverage",
            )
            absorbed = component.provider_cost if inserted else 0.0
        return {
            "settled_components": settled,
            "refunded_components": refunded,
            "absorbed_provider_cost": absorbed,
        }

    async def settle_call_feature(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        external_usage_id: str | None = None,
    ) -> PhoneCostComponent | None:
        """Settle a reserved one-call feature such as AMD exactly once."""

        normalized_provider = _normalize_provider(provider)
        normalized_component = _normalize_component(component_type)
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALL_COST_COMPONENTS
                WHERE call_id=? AND provider=? AND component_type=?
                ORDER BY id LIMIT 1
                """,
                (str(call_id), normalized_provider, normalized_component),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        component = _row_component(dict(row))
        if component.state == "settled":
            return component
        if component.state in {"reserved", "needs_attention"}:
            component = await self.mark_provider_started(component.id)
        if component.state != "provider_started":
            return component
        return await self.settle_component(
            component.id,
            actual_quantity=1.0,
            external_usage_id=external_usage_id,
        )

    async def reconcile_signed_twilio_duration(
        self,
        *,
        call_id: str,
        component_type: str,
        duration_seconds: float,
        external_usage_id: str,
    ) -> dict[str, float | int]:
        """Reconcile trusted signed Twilio PSTN/recording duration evidence."""

        component = _normalize_component(component_type)
        if component not in {"pstn", "recording"}:
            raise PhoneBillingError("Twilio duration component is invalid")
        return await self.settle_duration_components(
            call_id=call_id,
            provider="twilio",
            component_type=component,
            duration_seconds=duration_seconds,
            external_usage_id=str(external_usage_id),
        )

    async def reconcile_signed_twilio_amd(
        self,
        *,
        call_id: str,
        external_usage_id: str,
    ) -> PhoneCostComponent:
        """Settle AMD once, absorbing only provider work not pre-reserved."""

        return await self.reconcile_call_feature_callback(
            call_id=call_id,
            provider="twilio",
            component_type="amd",
            external_usage_id=external_usage_id,
        )

    async def reconcile_call_feature_callback(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        external_usage_id: str,
    ) -> PhoneCostComponent:
        """Reconcile one call feature with callback-first idempotency."""

        normalized_provider = _normalize_provider(provider)
        component = _normalize_component(component_type)
        if PHONE_COMPONENT_UNITS[component] != "call":
            raise PhoneBillingError("Telephone callback feature is not call-metered")
        external_id = str(external_usage_id or "").strip()
        if not external_id or len(external_id) > 400:
            raise PhoneBillingError("Telephone callback usage id is invalid")
        unreserved_key = f"{normalized_provider}:{component}:unreserved:{external_id}"
        async with self._connection_factory(readonly=True) as conn:
            cursor = await conn.execute(
                """
                SELECT * FROM PHONE_CALL_COST_COMPONENTS
                WHERE call_id=? AND provider=? AND component_type=?
                  AND (platform_absorbed=1 OR provider_confirmed_at IS NOT NULL)
                ORDER BY id LIMIT 1
                """,
                (str(call_id), normalized_provider, component),
            )
            confirmed = await cursor.fetchone()
        if confirmed is not None:
            return _row_component(dict(confirmed))

        settled = await self.settle_call_feature(
            call_id=call_id,
            provider=normalized_provider,
            component_type=component,
            external_usage_id=external_id,
        )
        if settled is not None:
            return settled
        recorded, _ = await self._record_confirmed_unreserved_usage(
            call_id=call_id,
            provider=normalized_provider,
            component_type=component,
            quantity=1,
            external_usage_id=external_id,
            dedupe_key=unreserved_key,
            reason=f"provider_{component}_was_not_reserved",
            canonical_call_feature=True,
        )
        return recorded

    async def _record_confirmed_unreserved_usage(
        self,
        *,
        call_id: str,
        provider: str,
        component_type: str,
        quantity: float,
        external_usage_id: str | None,
        dedupe_key: str,
        reason: str,
        canonical_call_feature: bool = False,
    ) -> tuple[PhoneCostComponent, bool]:
        """Persist confirmed late usage without retroactively debiting a payer."""

        normalized_provider = _normalize_provider(provider)
        component = _normalize_component(component_type)
        normalized_quantity = _positive_quantity(quantity)
        key = str(dedupe_key or "").strip()
        if not key or len(key) > 500:
            raise PhoneBillingError("Telephone billing dedupe key is invalid")
        async with self._connection_factory() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                call = await _load_call(conn, str(call_id))
                if canonical_call_feature:
                    cursor = await conn.execute(
                        """
                        SELECT * FROM PHONE_CALL_COST_COMPONENTS
                        WHERE call_id=? AND provider=? AND component_type=?
                          AND (
                            platform_absorbed=1
                            OR provider_confirmed_at IS NOT NULL
                          )
                        ORDER BY id LIMIT 1
                        """,
                        (str(call_id), normalized_provider, component),
                    )
                    confirmed = await cursor.fetchone()
                    if confirmed is not None:
                        await conn.commit()
                        return _row_component(dict(confirmed)), False
                try:
                    rate = await resolve_phone_billing_rate(
                        conn,
                        call=call,
                        provider=normalized_provider,
                        component_type=component,
                    )
                except PhoneBillingConfigurationError:
                    rate = None
                unit = PHONE_COMPONENT_UNITS[component]
                provider_rate = rate.provider_rate_per_unit if rate else 0.0
                customer_rate = rate.customer_rate_per_unit if rate else 0.0
                provider_cost = provider_rate * normalized_quantity
                insert_cursor = await conn.execute(
                    """
                    INSERT INTO PHONE_CALL_COST_COMPONENTS(
                        call_id,rate_id,provider,component_type,dedupe_key,
                        external_usage_id,quantity,reserved_quantity,unit,
                        provider_rate_per_unit,customer_rate_per_unit,
                        estimated_provider_cost,estimated_customer_charge,
                        final_provider_cost,final_customer_charge,
                        provider_cost,customer_charge,currency,state,
                        platform_absorbed,rate_missing,provider_confirmed_at,
                        settled_at,last_error
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                             'needs_attention',1,?,CURRENT_TIMESTAMP,
                             CURRENT_TIMESTAMP,?)
                    ON CONFLICT(call_id,dedupe_key) DO NOTHING
                    RETURNING *
                    """,
                    (
                        str(call_id),rate.id if rate else None,
                        normalized_provider,component,key,external_usage_id,
                        normalized_quantity,0,unit,provider_rate,customer_rate,
                        provider_cost,0,provider_cost,0,provider_cost,0,
                        rate.currency if rate else None,int(rate is None),
                        ("provider_usage_rate_missing" if rate is None else reason),
                    ),
                )
                row = await insert_cursor.fetchone()
                inserted = row is not None
                if row is None:
                    cursor = await conn.execute(
                        "SELECT * FROM PHONE_CALL_COST_COMPONENTS "
                        "WHERE call_id=? AND dedupe_key=?",
                        (str(call_id), key),
                    )
                    row = await cursor.fetchone()
                await self._refresh_call_totals(conn, str(call_id))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        if row is None:
            raise PhoneBillingError("Confirmed provider usage was not recorded")
        return _row_component(dict(row)), inserted

    async def _component_owner(
        self, component_id: int, *, conn: Any | None = None
    ) -> int:
        async def load(active: Any) -> int:
            cursor = await active.execute(
                """
                SELECT c.owner_user_id
                FROM PHONE_CALL_COST_COMPONENTS x
                JOIN PHONE_CALLS c ON c.id=x.call_id WHERE x.id=?
                """,
                (int(component_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise PhoneBillingError("Telephone cost component is unavailable")
            return int(row[0])

        if conn is not None:
            return await load(conn)
        async with self._connection_factory(readonly=True) as active:
            return await load(active)

    @staticmethod
    async def _existing_currency(conn: Any, call_id: str) -> str | None:
        cursor = await conn.execute(
            """
            SELECT currency FROM PHONE_CALL_COST_COMPONENTS
            WHERE call_id=? ORDER BY id LIMIT 1
            """,
            (call_id,),
        )
        row = await cursor.fetchone()
        return str(row[0]) if row is not None else None

    @staticmethod
    async def _refresh_call_totals(conn: Any, call_id: str) -> None:
        cursor = await conn.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN state<>'refunded' THEN customer_charge ELSE 0 END),0),
              COALESCE(SUM(CASE WHEN state='settled' THEN final_customer_charge ELSE 0 END),0),
              SUM(CASE
                    WHEN state IN ('reserved','provider_started') THEN 1
                    WHEN state='needs_attention' AND platform_absorbed=0 THEN 1
                    ELSE 0
                  END),
              MIN(currency),MAX(currency)
            FROM PHONE_CALL_COST_COMPONENTS WHERE call_id=?
            """,
            (call_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return
        if row[3] is not None and row[3] != row[4]:
            raise PhoneBillingConfigurationError(
                "A phone call cannot mix billing currencies"
            )
        cursor = await conn.execute(
            "SELECT status FROM PHONE_CALLS WHERE id=?",
            (call_id,),
        )
        call = await cursor.fetchone()
        terminal = call is not None and str(call[0]) in PHONE_TERMINAL_STATUSES
        final_cost = float(row[1]) if terminal and int(row[2] or 0) == 0 else None
        await conn.execute(
            """
            UPDATE PHONE_CALLS SET estimated_cost=?,final_cost=?,
                currency=COALESCE(?,currency),updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (float(row[0]), final_cost, row[3], call_id),
        )


class PhoneLiveBillingMeter:
    """Reserve short call/STT/transport tranches before more live I/O."""

    def __init__(
        self,
        call_id: str,
        *,
        service: PhoneBillingService | None = None,
        stream_attempt: int = 0,
        stream_tranche_seconds: int = DEFAULT_STREAM_TRANCHE_SECONDS,
        pstn_tranche_seconds: int = DEFAULT_PSTN_TRANCHE_SECONDS,
        close_buffer_seconds: int = DEFAULT_CLOSE_BUFFER_SECONDS,
        include_stt: bool = True,
        stt_provider: str = "elevenlabs",
    ) -> None:
        if not 1 <= int(stream_tranche_seconds) <= 300:
            raise ValueError("stream_tranche_seconds must be between 1 and 300")
        if not 1 <= int(pstn_tranche_seconds) <= 300:
            raise ValueError("pstn_tranche_seconds must be between 1 and 300")
        if int(stream_attempt) < 0:
            raise ValueError("stream_attempt cannot be negative")
        if not 1 <= int(close_buffer_seconds) < min(
            int(stream_tranche_seconds), int(pstn_tranche_seconds)
        ):
            raise ValueError("close_buffer_seconds must fit inside every tranche")
        self.call_id = str(call_id)
        self.service = service or PhoneBillingService()
        self.stream_attempt = int(stream_attempt)
        self.stream_tranche_seconds = int(stream_tranche_seconds)
        self.pstn_tranche_seconds = int(pstn_tranche_seconds)
        self.close_buffer_seconds = int(close_buffer_seconds)
        self.include_stt = bool(include_stt)
        self.stt_provider = _normalize_provider(stt_provider)
        self._covered_transport_index = -1
        self._covered_stt_index = -1
        self._covered_pstn_index = -1
        self._stt_audio_seconds = 0.0
        self._stt_provider_duration_seconds = 0.0
        self._stt_session_id: str | None = None
        self._coverage_lock = asyncio.Lock()

    async def reserve_outbound_provider_boundary(
        self, *, amd_enabled: bool
    ) -> tuple[PhoneCostComponent, ...]:
        components: list[PhoneCostComponent] = []
        try:
            components.append(
                await self.service.reserve_time_tranche(
                    call_id=self.call_id,
                    provider="twilio",
                    component_type="pstn",
                    tranche_index=0,
                    seconds=self.pstn_tranche_seconds,
                )
            )
            if amd_enabled:
                components.append(
                    await self.service.reserve_component(
                        call_id=self.call_id,
                        provider="twilio",
                        component_type="amd",
                        quantity=1,
                        dedupe_key="twilio:amd:call",
                    )
                )
        except BaseException:
            await self.refund_unstarted(
                tuple(components), reason="outbound_boundary_not_completed"
            )
            raise
        return tuple(components)

    async def reserve_media_boundary(self, *, inbound: bool) -> tuple[PhoneCostComponent, ...]:
        components: list[PhoneCostComponent] = []
        try:
            if inbound:
                components.append(
                    await self.service.reserve_time_tranche(
                        call_id=self.call_id,
                        provider="twilio",
                    component_type="pstn",
                    tranche_index=0,
                    seconds=self.pstn_tranche_seconds,
                    )
                )
            components.append(
                await self.service.reserve_time_tranche(
                    call_id=self.call_id,
                    provider="twilio",
                    component_type="transport",
                    tranche_index=0,
                    seconds=self.stream_tranche_seconds,
                    stream_attempt=self.stream_attempt,
                )
            )
            if self.include_stt:
                components.append(
                    await self.service.reserve_time_tranche(
                        call_id=self.call_id,
                        provider=self.stt_provider,
                        component_type="stt",
                        tranche_index=0,
                        seconds=self.stream_tranche_seconds,
                        stream_attempt=self.stream_attempt,
                    )
                )
        except BaseException:
            await self.refund_unstarted(
                tuple(components), reason="media_boundary_not_completed"
            )
            raise
        return tuple(components)

    async def start_components(
        self,
        components: tuple[PhoneCostComponent, ...],
        *,
        refundable_component_ids: set[int] | None = None,
    ) -> tuple[PhoneCostComponent, ...]:
        started: list[PhoneCostComponent] = []
        try:
            for component in components:
                started.append(
                    await self.service.mark_provider_started(component.id)
                )
        except BaseException as exc:
            for component in components[len(started) :]:
                if (
                    refundable_component_ids is not None
                    and component.id not in refundable_component_ids
                ):
                    continue
                try:
                    await self.service.refund_component_if_unstarted(
                        component.id, reason="provider_boundary_not_crossed"
                    )
                except Exception:
                    pass
            for component in started:
                try:
                    await self.service.mark_ambiguous(
                        component.id,
                        reason=f"provider_boundary_failed:{type(exc).__name__}",
                    )
                except Exception:
                    pass
            raise
        for component in started:
            try:
                tranche_index = int(component.dedupe_key.rsplit(":", 1)[1])
            except (AttributeError, TypeError, ValueError):
                continue
            if component.component_type == "pstn":
                self._covered_pstn_index = max(
                    self._covered_pstn_index,
                    tranche_index,
                )
            elif component.component_type in {"transport", "stt"}:
                if component.component_type == "transport":
                    self._covered_transport_index = max(
                        self._covered_transport_index,
                        tranche_index,
                    )
                else:
                    self._covered_stt_index = max(
                        self._covered_stt_index,
                        tranche_index,
                    )
        return tuple(started)

    async def refund_unstarted(
        self,
        components: tuple[PhoneCostComponent, ...],
        *,
        reason: str,
    ) -> None:
        for component in components:
            if component.state == "reserved":
                await self.service.refund_component_if_unstarted(
                    component.id, reason=reason
                )

    async def mark_started_ambiguous(
        self,
        components: tuple[PhoneCostComponent, ...],
        *,
        reason: str,
    ) -> None:
        for component in components:
            if component.state in {"reserved", "provider_started"}:
                await self.service.mark_ambiguous(component.id, reason=reason)

    async def ensure_live_coverage(
        self,
        *,
        elapsed_seconds: float | None = None,
        call_elapsed_seconds: float | None = None,
        stream_elapsed_seconds: float | None = None,
        include_pstn: bool = True,
    ) -> tuple[PhoneCostComponent, ...]:
        async with self._coverage_lock:
            return await self._ensure_live_coverage_locked(
                elapsed_seconds=elapsed_seconds,
                call_elapsed_seconds=call_elapsed_seconds,
                stream_elapsed_seconds=stream_elapsed_seconds,
                include_pstn=include_pstn,
            )

    async def _ensure_live_coverage_locked(
        self,
        *,
        elapsed_seconds: float | None,
        call_elapsed_seconds: float | None,
        stream_elapsed_seconds: float | None,
        include_pstn: bool,
    ) -> tuple[PhoneCostComponent, ...]:
        if elapsed_seconds is not None:
            if call_elapsed_seconds is None:
                call_elapsed_seconds = elapsed_seconds
            if stream_elapsed_seconds is None:
                stream_elapsed_seconds = elapsed_seconds
        if call_elapsed_seconds is None or stream_elapsed_seconds is None:
            raise PhoneBillingError("Live billing elapsed time is unavailable")
        call_elapsed = max(0.0, float(call_elapsed_seconds))
        stream_elapsed = max(0.0, float(stream_elapsed_seconds))
        stream_index = int(
            (stream_elapsed + self.close_buffer_seconds)
            // self.stream_tranche_seconds
        )
        pstn_index = int(
            (call_elapsed + self.close_buffer_seconds)
            // self.pstn_tranche_seconds
        )
        if (
            stream_index <= self._covered_transport_index
            and (
                not self.include_stt
                or stream_index <= self._covered_stt_index
            )
            and (
                not include_pstn
                or pstn_index <= self._covered_pstn_index
            )
        ):
            return ()
        components: list[PhoneCostComponent] = []
        created_component_ids: set[int] = set()
        try:
            if include_pstn and pstn_index > self._covered_pstn_index:
                for index in range(self._covered_pstn_index + 1, pstn_index + 1):
                    component, created = (
                        await self.service._reserve_time_tranche_owned(
                            call_id=self.call_id,
                            provider="twilio",
                            component_type="pstn",
                            tranche_index=index,
                            seconds=self.pstn_tranche_seconds,
                        )
                    )
                    components.append(component)
                    if created:
                        created_component_ids.add(component.id)
            first_stream_index = self._covered_transport_index + 1
            if self.include_stt:
                first_stream_index = (
                    min(
                        self._covered_transport_index,
                        self._covered_stt_index,
                    )
                    + 1
                )
            for index in range(first_stream_index, stream_index + 1):
                if index > self._covered_transport_index:
                    component, created = (
                        await self.service._reserve_time_tranche_owned(
                            call_id=self.call_id,
                            provider="twilio",
                            component_type="transport",
                            tranche_index=index,
                            seconds=self.stream_tranche_seconds,
                            stream_attempt=self.stream_attempt,
                        )
                    )
                    components.append(component)
                    if created:
                        created_component_ids.add(component.id)
                if self.include_stt and index > self._covered_stt_index:
                    component, created = (
                        await self.service._reserve_time_tranche_owned(
                            call_id=self.call_id,
                            provider=self.stt_provider,
                            component_type="stt",
                            tranche_index=index,
                            seconds=self.stream_tranche_seconds,
                            stream_attempt=self.stream_attempt,
                        )
                    )
                    components.append(component)
                    if created:
                        created_component_ids.add(component.id)
        except BaseException:
            for component in components:
                if (
                    component.id in created_component_ids
                    and component.state == "reserved"
                ):
                    await self.service.refund_component_if_unstarted(
                        component.id,
                        reason="live_coverage_not_completed",
                    )
            raise
        started = await self.start_components(
            tuple(components),
            refundable_component_ids=created_component_ids,
        )
        if include_pstn:
            self._covered_pstn_index = max(
                self._covered_pstn_index, pstn_index
            )
        self._covered_transport_index = max(
            self._covered_transport_index, stream_index
        )
        if self.include_stt:
            self._covered_stt_index = max(
                self._covered_stt_index, stream_index
            )
        return started

    def note_stt_audio_sent(self, byte_length: int) -> None:
        if not self.include_stt:
            return
        size = int(byte_length)
        if size > 0:
            # Media Streams supplies one unsigned 8-bit mu-law sample per byte
            # at 8 kHz. This is usage Aurvek can prove it sent to ElevenLabs.
            self._stt_audio_seconds += size / 8_000.0

    def note_stt_metadata(
        self, *, duration_seconds: float | None, session_id: str | None
    ) -> None:
        """Capture provider-reported usage metadata without provider coupling."""

        if not self.include_stt:
            return

        if duration_seconds is not None:
            value = float(duration_seconds)
            if math.isfinite(value) and value >= 0:
                self._stt_provider_duration_seconds = max(
                    self._stt_provider_duration_seconds, value
                )
        if session_id:
            self._stt_session_id = str(session_id)[:500]

    def note_deepgram_metadata(
        self, *, duration_seconds: float | None, request_id: str | None
    ) -> None:
        """Deprecated compatibility alias for callers migrating to generic STT."""

        self.note_stt_metadata(
            duration_seconds=duration_seconds,
            session_id=request_id,
        )

    async def finalize_stt_usage(self) -> dict[str, float | int] | None:
        if not self.include_stt:
            return None
        duration = max(
            self._stt_audio_seconds,
            self._stt_provider_duration_seconds,
        )
        if self._covered_stt_index < 0:
            return None
        return await self.service.settle_duration_components(
            call_id=self.call_id,
            provider=self.stt_provider,
            component_type="stt",
            duration_seconds=duration,
            external_usage_id=self._stt_session_id,
            stream_attempt=self.stream_attempt,
        )

    async def finalize_transport_usage(
        self,
        *,
        duration_seconds: float,
        external_usage_id: str | None = None,
    ) -> dict[str, float | int] | None:
        if self._covered_transport_index < 0:
            return None
        return await self.service.settle_duration_components(
            call_id=self.call_id,
            provider="twilio",
            component_type="transport",
            duration_seconds=max(0.0, float(duration_seconds)),
            external_usage_id=external_usage_id,
            stream_attempt=self.stream_attempt,
        )


class PhoneConnectBillingGate:
    """Reserve and claim initial PSTN/stream/STT coverage before ``<Connect>``."""

    def __init__(
        self,
        *,
        service: PhoneBillingService | None = None,
    ) -> None:
        self.service = service or PhoneBillingService()

    async def prepare(
        self,
        *,
        call_id: str,
        stream_attempt: int,
        call_elapsed_seconds: float,
        include_pstn: bool = True,
        include_stt: bool = True,
        stt_provider: str = "elevenlabs",
    ) -> tuple[PhoneCostComponent, ...]:
        meter = PhoneLiveBillingMeter(
            call_id,
            service=self.service,
            stream_attempt=stream_attempt,
            include_stt=include_stt,
            stt_provider=stt_provider,
        )
        return await meter.ensure_live_coverage(
            call_elapsed_seconds=max(0.0, float(call_elapsed_seconds)),
            stream_elapsed_seconds=0.0,
            include_pstn=include_pstn,
        )


async def recover_stale_phone_billing(
    *,
    connection_factory: ConnectionFactory = get_db_connection,
    stale_before: datetime | None = None,
) -> dict[str, int]:
    """Refund safe pre-I/O holds and surface every ambiguous stale boundary."""

    threshold = stale_before or (datetime.now(UTC) - timedelta(minutes=15))
    threshold_text = threshold.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    service = PhoneBillingService(connection_factory=connection_factory)
    orphan_refunded = 0
    # The reservation is created just before its component.  A process crash
    # in that narrow gap leaves no ledger row to drive normal recovery.  Hold
    # the SQLite writer lock while both proving the absence and refunding so a
    # concurrent component insert cannot revive a refunded reservation.
    async with connection_factory() as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await conn.execute(
                """
                SELECT r.id,r.user_id
                FROM BILLING_USAGE_RESERVATIONS r
                WHERE r.purpose='phone' AND r.status='active'
                  AND r.provider_started_at IS NULL AND r.created_at<?
                  AND NOT EXISTS(
                    SELECT 1 FROM PHONE_CALL_COST_COMPONENTS x
                    WHERE x.billing_reservation_id=r.id
                  )
                ORDER BY r.created_at,r.id
                """,
                (threshold_text,),
            )
            for reservation_id, user_id in await cursor.fetchall():
                if await refund_fixed_usage_in_transaction(
                    conn,
                    str(reservation_id),
                    expected_user_id=int(user_id),
                ):
                    orphan_refunded += 1
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise
    async with connection_factory(readonly=True) as conn:
        cursor = await conn.execute(
            """
            SELECT x.id,x.state,r.provider_started_at
            FROM PHONE_CALL_COST_COMPONENTS x
            LEFT JOIN BILLING_USAGE_RESERVATIONS r
              ON r.id=x.billing_reservation_id
            WHERE x.state IN ('reserved','provider_started')
              AND x.updated_at<? ORDER BY x.id
            """,
            (threshold_text,),
        )
        rows = [tuple(row) for row in await cursor.fetchall()]
    refunded = 0
    attention = 0
    for component_id, state, provider_started_at in rows:
        if str(state) == "reserved" and provider_started_at is None:
            await service.refund_component(
                int(component_id), reason="stale_before_provider_boundary"
            )
            refunded += 1
        else:
            await service.mark_ambiguous(
                int(component_id), reason="stale_after_provider_boundary"
            )
            attention += 1
    return {
        "refunded": refunded + orphan_refunded,
        "orphan_refunded": orphan_refunded,
        "needs_attention": attention,
    }


def _utc_text() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "DEFAULT_PSTN_TRANCHE_SECONDS",
    "DEFAULT_STREAM_TRANCHE_SECONDS",
    "DEFAULT_CLOSE_BUFFER_SECONDS",
    "PHONE_COMPONENT_TYPES",
    "PHONE_COMPONENT_UNITS",
    "PhoneBillingAmbiguous",
    "PhoneBillingConfigurationError",
    "PhoneBillingError",
    "PhoneBillingExhausted",
    "PhoneBillingRate",
    "PhoneBillingService",
    "PhoneCostComponent",
    "PhoneConnectBillingGate",
    "PhoneLiveBillingMeter",
    "country_for_e164",
    "phone_billing_readiness",
    "recover_stale_phone_billing",
    "resolve_phone_billing_rate",
    "upsert_phone_billing_rate",
]
