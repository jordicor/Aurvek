"""Durable one-shot dispatcher for outbound Twilio calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
import json
from typing import Any
from uuid import uuid4

from integrations.telephony.async_shutdown import cancel_and_join_tasks
from integrations.telephony.billing import (
    PhoneBillingConfigurationError,
    PhoneBillingError,
    PhoneBillingExhausted,
    PhoneCostComponent,
    PhoneLiveBillingMeter,
)
from integrations.telephony.config import TelephonyConfig, load_telephony_config
from integrations.telephony.repository import (
    TelephonyConflictError,
    TelephonyRepository,
    TelephonyStateError,
)
from integrations.telephony.twilio_client import (
    AsyncTwilioVoiceClient,
    CallDispatchOutcome,
    CallDispatchResult,
    CreateCallRequest,
    DispatchUnknownReason,
    TwilioVoiceAPIError,
)


Clock = Callable[[], datetime]
CallbackURLFactory = Callable[[str], str]
IdentifierFactory = Callable[[str], str]
ConfigLoader = Callable[[], Awaitable[TelephonyConfig]]
DestinationCountryLoader = Callable[[dict[str, Any]], str]
DispatchFence = Callable[[str, dict[str, Any] | None], Awaitable[bool]]
BillingMeterFactory = Callable[[str], PhoneLiveBillingMeter]
_DISPATCH_SHUTDOWN_GRACE_SECONDS = 3.0


async def _open_dispatch_fence(
    _dispatcher_id: str,
    _call: dict[str, Any] | None = None,
) -> bool:
    return True


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("dispatcher clock must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _utc_text(value: datetime) -> str:
    return _utc(value).isoformat(timespec="seconds").replace("+00:00", "Z")


def _new_identifier(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex}"


def _snapshot_destination_country(job: dict[str, Any]) -> str:
    snapshot = json.loads(str(job["config_snapshot_json"]))
    country = str(snapshot["destination_country"]).strip().upper()
    if len(country) != 2 or not country.isalpha():
        raise ValueError("destination_country is invalid")
    return country


class DispatchDisposition(StrEnum):
    ACCEPTED = "accepted"
    DISPATCH_UNKNOWN = "dispatch_unknown"
    FAILED = "failed"
    CONFLICT = "conflict"
    MISSED = "missed"
    LOST_LEASE = "lost_lease"


@dataclass(frozen=True, slots=True)
class DispatchAttempt:
    job_id: str
    call_id: str | None
    disposition: DispatchDisposition
    provider_call_sid: str | None = None
    detail_code: str | None = None


@dataclass(frozen=True, slots=True)
class DispatcherHealth:
    dispatcher_id: str
    started_at_utc: str
    last_heartbeat_at_utc: str
    dispatcher_epoch: int = 0
    live_since_utc: str | None = None
    heartbeat_lease_until_utc: str | None = None
    last_scan_at_utc: str | None = None
    last_success_at_utc: str | None = None
    last_error: str | None = None
    scans: int = 0
    jobs_claimed: int = 0
    accepted: int = 0
    dispatch_unknown: int = 0
    failed: int = 0
    conflicts: int = 0

    @property
    def healthy(self) -> bool:
        return self.last_error is None


class OutboundCallDispatcher:
    """Claim due database jobs and issue exactly one provider POST per claim."""

    def __init__(
        self,
        repository: TelephonyRepository,
        twilio_client: AsyncTwilioVoiceClient,
        *,
        dispatcher_id: str,
        twiml_url_factory: CallbackURLFactory,
        status_callback_url_factory: CallbackURLFactory,
        amd_status_callback_url_factory: CallbackURLFactory | None = None,
        clock: Clock = _utc_now,
        identifier_factory: IdentifierFactory = _new_identifier,
        jitter_seconds: int = 10,
        job_lease_seconds: int = 60,
        foreground_lease_seconds: int = 120,
        reconcile_window_seconds: int = 900,
        poll_interval_seconds: float = 1.0,
        scan_limit: int = 100,
        max_concurrent_dispatches: int | None = None,
        heartbeat_ttl_seconds: int = 30,
        config_loader: ConfigLoader = load_telephony_config,
        destination_country_loader: DestinationCountryLoader = (
            _snapshot_destination_country
        ),
        dispatch_fence: DispatchFence = _open_dispatch_fence,
        billing_meter_factory: BillingMeterFactory = PhoneLiveBillingMeter,
    ) -> None:
        owner = str(dispatcher_id or "").strip()
        if not owner:
            raise ValueError("dispatcher_id is required")
        if not 0 <= int(jitter_seconds) <= 60:
            raise ValueError("jitter_seconds must be between 0 and 60")
        if int(job_lease_seconds) <= 30:
            raise ValueError("job_lease_seconds must exceed the provider timeout")
        if int(foreground_lease_seconds) <= 0:
            raise ValueError("foreground_lease_seconds must be positive")
        if int(reconcile_window_seconds) <= 0:
            raise ValueError("reconcile_window_seconds must be positive")
        if float(poll_interval_seconds) <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if not 1 <= int(scan_limit) <= 1_000:
            raise ValueError("scan_limit must be between 1 and 1000")
        dispatch_concurrency = (
            int(scan_limit)
            if max_concurrent_dispatches is None
            else int(max_concurrent_dispatches)
        )
        if not 1 <= dispatch_concurrency <= int(scan_limit):
            raise ValueError(
                "max_concurrent_dispatches must be between 1 and scan_limit"
            )
        if int(heartbeat_ttl_seconds) <= int(jitter_seconds):
            raise ValueError("heartbeat_ttl_seconds must exceed jitter_seconds")

        self.repository = repository
        self.twilio_client = twilio_client
        self.dispatcher_id = owner
        self._twiml_url_factory = twiml_url_factory
        self._status_callback_url_factory = status_callback_url_factory
        self._amd_status_callback_url_factory = amd_status_callback_url_factory
        self._clock = clock
        self._identifier_factory = identifier_factory
        self._config_loader = config_loader
        self._destination_country_loader = destination_country_loader
        self._dispatch_fence = dispatch_fence
        self._billing_meter_factory = billing_meter_factory
        self.jitter_seconds = int(jitter_seconds)
        self.job_lease_seconds = int(job_lease_seconds)
        self.foreground_lease_seconds = int(foreground_lease_seconds)
        self.reconcile_window_seconds = int(reconcile_window_seconds)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.scan_limit = int(scan_limit)
        self.max_concurrent_dispatches = dispatch_concurrency
        self.heartbeat_ttl_seconds = int(heartbeat_ttl_seconds)
        self._lease_owner = owner
        self._live_since_utc: str | None = None
        self._dispatch_semaphore = asyncio.Semaphore(dispatch_concurrency)

        started = _utc_text(self._clock())
        self._health = DispatcherHealth(
            dispatcher_id=owner,
            started_at_utc=started,
            last_heartbeat_at_utc=started,
        )

    def health(self) -> DispatcherHealth:
        """Return a stable, user-visible snapshot of scheduler liveness."""

        return replace(self._health)

    async def run_once(self, *, max_jobs: int | None = None) -> tuple[DispatchAttempt, ...]:
        """Drain a bounded due-job batch without sleeping."""

        batch_limit = self.scan_limit if max_jobs is None else int(max_jobs)
        if not 1 <= batch_limit <= self.scan_limit:
            raise ValueError("max_jobs is outside the configured scan limit")
        attempts: list[DispatchAttempt] = []
        remaining = batch_limit
        try:
            while remaining > 0:
                round_limit = min(remaining, self.max_concurrent_dispatches)
                claimed_jobs = await self._claim_due_jobs(round_limit)
                if not claimed_jobs:
                    break
                results = await asyncio.gather(
                    *(
                        self._dispatch_claimed_with_lease_renewal(claimed)
                        for claimed in claimed_jobs
                    ),
                    return_exceptions=True,
                )
                first_error: BaseException | None = None
                for result in results:
                    if isinstance(result, BaseException):
                        first_error = first_error or result
                        continue
                    attempts.append(result)
                    self._count_attempt(result)
                if first_error is not None:
                    raise first_error
                remaining -= len(claimed_jobs)
                if len(claimed_jobs) < round_limit:
                    break
            finished_at = _utc(self._clock())
            await self._heartbeat(finished_at)
            finished = _utc_text(finished_at)
            self._health = replace(
                self._health,
                last_success_at_utc=finished,
            )
            return tuple(attempts)
        except Exception as exc:
            self._health = replace(
                self._health,
                last_heartbeat_at_utc=_utc_text(self._clock()),
                last_error=type(exc).__name__,
            )
            raise

    async def dispatch_job(self, job_id: str) -> DispatchAttempt | None:
        """Claim and dispatch one known job, useful for deterministic wakeups."""

        now = _utc(self._clock())
        await self._heartbeat(now)
        claimed = await self.repository.claim_job(
            job_id=job_id,
            lease_owner=self._lease_owner,
            lease_until=_utc_text(now + timedelta(seconds=self.job_lease_seconds)),
            now_utc=_utc_text(now),
            dispatcher_started_at_utc=self._live_since_utc,
            dispatcher_id=self.dispatcher_id,
            jitter_seconds=self.jitter_seconds,
            reconcile_deadline=_utc_text(
                now + timedelta(seconds=self.reconcile_window_seconds)
            ),
        )
        if claimed is None:
            self._health = replace(
                self._health,
                last_success_at_utc=_utc_text(self._clock()),
                last_error=None,
            )
            return None
        self._health = replace(
            self._health,
            jobs_claimed=self._health.jobs_claimed + 1,
        )
        attempt = await self._dispatch_claimed_with_lease_renewal(claimed)
        self._count_attempt(attempt)
        finished_at = _utc(self._clock())
        await self._heartbeat(finished_at)
        finished = _utc_text(finished_at)
        self._health = replace(
            self._health,
            last_heartbeat_at_utc=finished,
            last_success_at_utc=finished,
            last_error=None,
        )
        return attempt

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        """Keep scanning/heartbeating while bounded dispatches run separately."""

        dispatches: set[asyncio.Task[DispatchAttempt]] = set()
        caught: BaseException | None = None
        try:
            while not stop_event.is_set():
                for completed in tuple(
                    task for task in dispatches if task.done()
                ):
                    dispatches.remove(completed)
                    attempt = completed.result()
                    self._count_attempt(attempt)
                available = self.max_concurrent_dispatches - len(dispatches)
                claimed_jobs = await self._claim_due_jobs(
                    min(self.scan_limit, max(0, available))
                )
                dispatches.update(
                    asyncio.create_task(
                        self._dispatch_claimed_with_lease_renewal(claimed)
                    )
                    for claimed in claimed_jobs
                )
                self._health = replace(
                    self._health,
                    last_success_at_utc=_utc_text(self._clock()),
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                except TimeoutError:
                    continue
        except BaseException as exc:
            caught = exc
        finally:
            # A dispatcher lifecycle fence owns every admitted child.  Waiting
            # for in-flight provider work after stop/cancellation would let a
            # child cross the POST boundary after the memory outbox or runtime
            # has already failed closed.
            if dispatches:
                survivors = await cancel_and_join_tasks(
                    dispatches,
                    deadline=(
                        asyncio.get_running_loop().time()
                        + _DISPATCH_SHUTDOWN_GRACE_SECONDS
                    ),
                )
                if survivors:
                    self._health = replace(
                        self._health,
                        last_error="dispatch_shutdown_grace_exceeded",
                    )
        if caught is not None:
            raise caught

    async def _claim_due_jobs(
        self,
        batch_limit: int,
    ) -> list[dict[str, Any]]:
        scan_time = _utc(self._clock())
        scan_text = _utc_text(scan_time)
        await self._heartbeat(scan_time)
        self._health = replace(
            self._health,
            last_scan_at_utc=scan_text,
            scans=self._health.scans + 1,
            last_error=None,
        )
        await self.repository.expire_dispatch_unknown(
            now_utc=scan_text,
            limit=max(1, batch_limit),
        )
        claimed_jobs: list[dict[str, Any]] = []
        for _ in range(batch_limit):
            claimed = await self.repository.claim_next_due_job(
                lease_owner=self._lease_owner,
                lease_until=_utc_text(
                    scan_time + timedelta(seconds=self.job_lease_seconds)
                ),
                now_utc=scan_text,
                dispatcher_started_at_utc=self._live_since_utc,
                dispatcher_id=self.dispatcher_id,
                jitter_seconds=self.jitter_seconds,
                reconcile_deadline=_utc_text(
                    scan_time + timedelta(seconds=self.reconcile_window_seconds)
                ),
                scan_limit=self.scan_limit,
            )
            if claimed is None:
                break
            claimed_jobs.append(claimed)
            self._health = replace(
                self._health,
                jobs_claimed=self._health.jobs_claimed + 1,
            )
        return claimed_jobs

    async def _dispatch_claimed_job(
        self,
        job: dict[str, Any],
    ) -> DispatchAttempt:
        job_id = str(job["id"])
        lease_owner = str(job["lease_owner"])
        lease_token = str(job["lease_token"])
        now = _utc(self._clock())
        call_id = self._identifier("phone-call")
        dispatch_token = self._identifier("dispatch")
        try:
            if await self.repository.miss_unstarted_dispatch_if_late(
                job_id=job_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                now_utc=_utc_text(now),
                jitter_seconds=self.jitter_seconds,
            ):
                return DispatchAttempt(
                    job_id=job_id,
                    call_id=None,
                    disposition=DispatchDisposition.MISSED,
                    detail_code="job_missed",
                )
            policy = await self._validate_current_dispatch_policy(job)
            if policy is not None:
                target, detail_code, detail = policy
                await self.repository.transition_job(
                    job_id,
                    target,
                    error_code=detail_code,
                    error_detail=detail,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    now_utc=_utc_text(now),
                )
                return DispatchAttempt(
                    job_id=job_id,
                    call_id=None,
                    disposition=(
                        DispatchDisposition.CONFLICT
                        if target == "canceled"
                        else DispatchDisposition.FAILED
                    ),
                    detail_code=detail_code,
                )
            call, _created = await self.repository.create_call_from_job(
                job_id=job_id,
                call_id=call_id,
                dispatch_token=dispatch_token,
                foreground_lease_owner=lease_owner,
                foreground_lease_until=_utc_text(
                    now + timedelta(seconds=self.foreground_lease_seconds)
                ),
                lease_owner=lease_owner,
                lease_token=lease_token,
                now_utc=_utc_text(now),
                recover_existing_foreground=True,
            )
        except TelephonyConflictError as exc:
            conflict_message = str(exc)
            detail_code = (
                "active_call_conflict"
                if "incompatible phone call" in conflict_message.lower()
                or "foreground is already owned" in conflict_message.lower()
                else "dispatch_conflict"
            )

            await self.repository.transition_job(
                job_id,
                "conflict",
                error_code=detail_code,
                error_detail=conflict_message,
                lease_owner=lease_owner,
                lease_token=lease_token,
                now_utc=_utc_text(now),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=None,
                disposition=DispatchDisposition.CONFLICT,
                detail_code=detail_code,
            )
        except TelephonyStateError:
            return DispatchAttempt(
                job_id=job_id,
                call_id=None,
                disposition=DispatchDisposition.LOST_LEASE,
                detail_code="lost_lease_before_call",
            )

        call_id = str(call["id"])
        dispatch_token = str(call["dispatch_token"])
        try:
            amd_enabled = bool(call["amd_enabled"])
            amd_callback_url = None
            if amd_enabled:
                if self._amd_status_callback_url_factory is None:
                    raise ValueError("AMD callback URL is not configured")
                amd_callback_url = self._amd_status_callback_url_factory(
                    dispatch_token
                )
            request = CreateCallRequest(
                from_e164=str(call["from_e164"]),
                to_e164=str(call["to_e164"]),
                twiml_url=self._twiml_url_factory(dispatch_token),
                status_callback_url=self._status_callback_url_factory(dispatch_token),
                amd_enabled=amd_enabled,
                amd_status_callback_url=amd_callback_url,
            )
        except (TypeError, ValueError):
            await self.repository.fail_unstarted_dispatch(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code="invalid_dispatch_configuration",
                error_detail="Outbound callback URL configuration is invalid",
                now_utc=_utc_text(self._clock()),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.FAILED,
                detail_code="invalid_dispatch_configuration",
            )

        billing_meter = self._billing_meter_factory(call_id)
        try:
            billing_components = await billing_meter.reserve_outbound_provider_boundary(
                amd_enabled=amd_enabled
            )
        except PhoneBillingExhausted:
            await self.repository.fail_unstarted_dispatch(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code="insufficient_phone_balance",
                error_detail="Telephone provider usage could not be reserved",
                now_utc=_utc_text(self._clock()),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.FAILED,
                detail_code="insufficient_phone_balance",
            )
        except (PhoneBillingConfigurationError, PhoneBillingError):
            await self.repository.fail_unstarted_dispatch(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code="phone_billing_not_ready",
                error_detail="Telephone billing configuration is unavailable",
                now_utc=_utc_text(self._clock()),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.FAILED,
                detail_code="phone_billing_not_ready",
            )

        boundary_time = _utc(self._clock())
        foreground_fencing_token = int(call["foreground_fencing_token"])
        foreground_renewed = await self.repository.renew_foreground_lease(
            call_id=call_id,
            lease_owner=lease_owner,
            fencing_token=foreground_fencing_token,
            lease_until=_utc_text(
                boundary_time + timedelta(seconds=self.foreground_lease_seconds)
            ),
            now_utc=_utc_text(boundary_time),
        )
        if not foreground_renewed:
            await billing_meter.refund_unstarted(
                billing_components, reason="lost_foreground_before_post"
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.LOST_LEASE,
                detail_code="lost_foreground_before_post",
            )
        if not await self._dispatch_fence(self.dispatcher_id, call):
            try:
                await self.repository.fail_unstarted_dispatch(
                    job_id=job_id,
                    call_id=call_id,
                    lease_owner=lease_owner,
                    lease_token=lease_token,
                    error_code="runtime_fenced_before_provider",
                    error_detail=(
                        "Telephone runtime stopped before the provider boundary"
                    ),
                    now_utc=_utc_text(boundary_time),
                )
            except TelephonyStateError:
                await billing_meter.refund_unstarted(
                    billing_components,
                    reason="lost_lease_before_provider_fence",
                )
                return DispatchAttempt(
                    job_id=job_id,
                    call_id=call_id,
                    disposition=DispatchDisposition.LOST_LEASE,
                    detail_code="lost_lease_before_provider_fence",
                )
            await billing_meter.refund_unstarted(
                billing_components,
                reason="runtime_fenced_before_provider",
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.FAILED,
                detail_code="runtime_fenced_before_provider",
            )
        crossed = await self.repository.mark_provider_request_started(
            call_id=call_id,
            dispatch_token=dispatch_token,
            lease_owner=lease_owner,
            lease_token=lease_token,
            foreground_lease_owner=lease_owner,
            foreground_fencing_token=foreground_fencing_token,
            now_utc=_utc_text(boundary_time),
            jitter_seconds=self.jitter_seconds,
            reconcile_deadline=_utc_text(
                boundary_time + timedelta(seconds=self.reconcile_window_seconds)
            ),
        )
        if not crossed:
            await billing_meter.refund_unstarted(
                billing_components, reason="provider_boundary_not_crossed"
            )
            deadline = _utc(
                datetime.fromisoformat(
                    str(job["scheduled_at_utc"]).replace("Z", "+00:00")
                )
            ) + timedelta(seconds=self.jitter_seconds)
            if boundary_time > deadline:
                return DispatchAttempt(
                    job_id=job_id,
                    call_id=call_id,
                    disposition=DispatchDisposition.MISSED,
                    detail_code="job_missed",
                )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.LOST_LEASE,
                detail_code="lost_lease_before_post",
            )

        return await self._issue_provider_request_after_boundary(
            job_id=job_id,
            call_id=call_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            request=request,
            billing_meter=billing_meter,
            billing_components=billing_components,
            call=call,
        )

    async def _issue_provider_request_after_boundary(
        self,
        *,
        job_id: str,
        call_id: str,
        lease_owner: str,
        lease_token: str,
        request: CreateCallRequest,
        billing_meter: PhoneLiveBillingMeter,
        billing_components: tuple[PhoneCostComponent, ...],
        call: dict[str, Any],
    ) -> DispatchAttempt:
        started_components: tuple[PhoneCostComponent, ...] = ()
        try:
            started_components = await billing_meter.start_components(
                billing_components
            )
        except Exception as exc:
            outcome_time = _utc(self._clock())
            await self.repository.mark_provider_dispatch_unknown(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code="dispatch_unknown_billing_boundary",
                error_detail=(
                    "Telephone billing boundary failed before provider POST: "
                    f"{type(exc).__name__}"
                ),
                reconcile_deadline=_utc_text(
                    outcome_time
                    + timedelta(seconds=self.reconcile_window_seconds)
                ),
                now_utc=_utc_text(outcome_time),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.DISPATCH_UNKNOWN,
                detail_code=(
                    "dispatch_unknown_billing_boundary_"
                    f"{type(exc).__name__.lower()}"
                ),
            )

        # This is the final lifecycle/configuration fence.  Billing writes and
        # every other awaited prerequisite are complete above; on the true
        # branch the single provider POST is the very next await.
        if not await self._dispatch_fence(self.dispatcher_id, call):
            for component in started_components:
                try:
                    await billing_meter.service.refund_component(
                        component.id,
                        reason="runtime_fenced_after_billing_boundary",
                    )
                except Exception:
                    try:
                        await billing_meter.service.mark_ambiguous(
                            component.id,
                            reason="runtime_fence_refund_failed",
                        )
                    except Exception:
                        pass
            outcome_time = _utc(self._clock())
            await self.repository.mark_provider_dispatch_unknown(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code="dispatch_unknown",
                error_detail=(
                    "Telephone runtime stopped after the durable billing "
                    "boundary and before provider POST"
                ),
                reconcile_deadline=_utc_text(
                    outcome_time
                    + timedelta(seconds=self.reconcile_window_seconds)
                ),
                now_utc=_utc_text(outcome_time),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.DISPATCH_UNKNOWN,
                detail_code="dispatch_unknown_runtime_fenced",
            )

        try:
            result = await self.twilio_client.create_call_once(request)
        except TwilioVoiceAPIError as exc:
            for component in started_components:
                await billing_meter.service.refund_component(
                    component.id,
                    reason=f"provider_rejected:{exc.status_code}",
                )
            outcome_time = _utc(self._clock())
            code = f"twilio_http_{exc.status_code}"
            if exc.provider_code is not None:
                code += f"_{exc.provider_code}"
            await self.repository.fail_provider_dispatch(
                job_id=job_id,
                call_id=call_id,
                lease_owner=lease_owner,
                lease_token=lease_token,
                error_code=code,
                error_detail=str(exc),
                now_utc=_utc_text(outcome_time),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.FAILED,
                detail_code=code,
            )
        except Exception as exc:
            if started_components:
                await billing_meter.mark_started_ambiguous(
                    started_components,
                    reason=f"provider_dispatch_ambiguous:{type(exc).__name__}",
                )
            result = CallDispatchResult(
                outcome=CallDispatchOutcome.DISPATCH_UNKNOWN,
                unknown_reason=DispatchUnknownReason.TRANSPORT_ERROR,
            )
            unexpected_detail = f"Provider request raised {type(exc).__name__}"
        else:
            unexpected_detail = None

        outcome_time = _utc(self._clock())
        if result.outcome is CallDispatchOutcome.ACCEPTED and result.call_sid:
            await self.repository.complete_provider_dispatch(
                job_id=job_id,
                call_id=call_id,
                provider_call_sid=result.call_sid,
                lease_owner=lease_owner,
                lease_token=lease_token,
                now_utc=_utc_text(outcome_time),
            )
            return DispatchAttempt(
                job_id=job_id,
                call_id=call_id,
                disposition=DispatchDisposition.ACCEPTED,
                provider_call_sid=result.call_sid,
            )

        reason = result.unknown_reason or DispatchUnknownReason.INVALID_RESPONSE
        await billing_meter.mark_started_ambiguous(
            started_components,
            reason=f"provider_dispatch_ambiguous:{reason.value}",
        )
        detail_code = f"dispatch_unknown_{reason.value}"
        await self.repository.mark_provider_dispatch_unknown(
            job_id=job_id,
            call_id=call_id,
            lease_owner=lease_owner,
            lease_token=lease_token,
            error_code=detail_code,
            error_detail=unexpected_detail or "Provider dispatch result was ambiguous",
            reconcile_deadline=_utc_text(
                outcome_time + timedelta(seconds=self.reconcile_window_seconds)
            ),
            now_utc=_utc_text(outcome_time),
        )
        return DispatchAttempt(
            job_id=job_id,
            call_id=call_id,
            disposition=DispatchDisposition.DISPATCH_UNKNOWN,
            detail_code=detail_code,
        )

    async def _validate_current_dispatch_policy(
        self, job: dict[str, Any]
    ) -> tuple[str, str, str] | None:
        """Revalidate mutable admin ceilings immediately before Call creation."""

        try:
            config = await self._config_loader()
        except Exception:
            return (
                "needs_attention",
                "telephony_config_unavailable",
                "Current telephony policy could not be verified",
            )
        if not config.enabled:
            return (
                "canceled",
                "telephony_disabled",
                "Telephony was disabled before this call became due",
            )
        try:
            destination_country = self._destination_country_loader(job)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return (
                "needs_attention",
                "destination_country_unavailable",
                "Scheduled destination country could not be verified",
            )
        if destination_country not in config.allowed_countries:
            return (
                "canceled",
                "destination_country_disallowed",
                "Destination country is no longer allowed by administration",
            )
        return None

    async def _dispatch_claimed_with_lease_renewal(
        self,
        job: dict[str, Any],
    ) -> DispatchAttempt:
        async with self._dispatch_semaphore:
            stop_renewal = asyncio.Event()
            renewal = asyncio.create_task(
                self._renew_claim_until_stopped(job, stop_renewal)
            )
            try:
                return await self._dispatch_claimed_job(job)
            finally:
                stop_renewal.set()
                survivors = await cancel_and_join_tasks(
                    (renewal,),
                    deadline=(
                        asyncio.get_running_loop().time()
                        + _DISPATCH_SHUTDOWN_GRACE_SECONDS
                    ),
                )
                if survivors:
                    self._health = replace(
                        self._health,
                        last_error="lease_renewal_shutdown_grace_exceeded",
                    )

    async def _renew_claim_until_stopped(
        self,
        job: dict[str, Any],
        stop_event: asyncio.Event,
    ) -> None:
        interval = max(1.0, min(20.0, self.job_lease_seconds / 3))
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            now = _utc(self._clock())
            renewed = await self.repository.renew_job_lease(
                job_id=str(job["id"]),
                lease_owner=str(job["lease_owner"]),
                lease_token=str(job["lease_token"]),
                lease_until=_utc_text(
                    now + timedelta(seconds=self.job_lease_seconds)
                ),
                now_utc=_utc_text(now),
            )
            if not renewed:
                return

    async def _heartbeat(self, now: datetime) -> None:
        heartbeat_at = _utc(now)
        lease_until = heartbeat_at + timedelta(seconds=self.heartbeat_ttl_seconds)
        durable = await self.repository.heartbeat_dispatcher(
            dispatcher_id=self.dispatcher_id,
            started_at_utc=self._health.started_at_utc,
            heartbeat_at_utc=_utc_text(heartbeat_at),
            lease_until_utc=_utc_text(lease_until),
        )
        epoch = int(durable["epoch"])
        self._live_since_utc = str(durable["live_since_utc"])
        self._lease_owner = f"{self.dispatcher_id}:{epoch}"
        self._health = replace(
            self._health,
            dispatcher_epoch=epoch,
            live_since_utc=self._live_since_utc,
            last_heartbeat_at_utc=str(durable["heartbeat_at_utc"]),
            heartbeat_lease_until_utc=str(durable["lease_until_utc"]),
        )

    def _identifier(self, prefix: str) -> str:
        value = str(self._identifier_factory(prefix) or "").strip()
        if not value:
            raise ValueError("identifier_factory returned an empty identifier")
        return value

    def _count_attempt(self, attempt: DispatchAttempt) -> None:
        changes: dict[str, int] = {}
        if attempt.disposition is DispatchDisposition.ACCEPTED:
            changes["accepted"] = self._health.accepted + 1
        elif attempt.disposition is DispatchDisposition.DISPATCH_UNKNOWN:
            changes["dispatch_unknown"] = self._health.dispatch_unknown + 1
        elif attempt.disposition is DispatchDisposition.FAILED:
            changes["failed"] = self._health.failed + 1
        elif attempt.disposition is DispatchDisposition.CONFLICT:
            changes["conflicts"] = self._health.conflicts + 1
        if changes:
            self._health = replace(self._health, **changes)


__all__ = [
    "DispatchAttempt",
    "DispatchDisposition",
    "DispatcherHealth",
    "OutboundCallDispatcher",
]
