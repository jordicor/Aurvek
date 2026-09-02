"""Process lifecycle for Aurvek's native telephone channel.

The provider routes stay registered even when the feature is disabled so that
already-issued, correctly signed terminal callbacks can still reconcile.  Only
an enabled and fully ready runtime starts the outbound dispatcher.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import os
import socket
from typing import Any

from database import get_db_connection
from integrations.telephony.async_shutdown import cancel_and_join_tasks
from integrations.telephony.audio_cache_service import (
    PhoneAudioCacheService,
    RenderedMp3,
)
from integrations.telephony.billing import (
    phone_billing_readiness,
    recover_stale_phone_billing,
)
from integrations.telephony.ai_call_outbox import AiCallStartOutboxWorker
from integrations.telephony.config import TelephonyConfig, load_telephony_config
from integrations.telephony.ffmpeg import is_ffmpeg_available
from integrations.telephony.memory_outbox import PhoneMemoryOutboxWorker
from integrations.telephony.purge import (
    PhoneDataPurgeWorker,
    create_phone_data_purge_worker,
)
from integrations.telephony.repository import TelephonyRepository
from integrations.telephony.routes import (
    VOICE_AMD_PATH,
    VOICE_STATUS_PATH,
    VOICE_TWIML_PATH,
    TelephonyProviderRuntime,
    get_provider_runtime,
)
from integrations.telephony.scheduler import OutboundCallDispatcher
from integrations.telephony.security import canonical_twilio_url
from log_config import logger
from tools.tts_load_balancer import (
    elevenlabs_keys_ready,
    has_elevenlabs_keys,
    prepare_elevenlabs_keys,
)


ConfigLoader = Callable[[], Awaitable[TelephonyConfig]]
ReadinessProbe = Callable[[], Awaitable[str | None]]
KeyAvailabilityProbe = Callable[[], bool]
KeyReadinessProbe = Callable[[], bool]
KeyPrepareProbe = Callable[[], bool]
FfmpegProbe = Callable[[], Awaitable[bool]]
StaleRecovery = Callable[[], Awaitable[int]]
DispatcherFactory = Callable[..., OutboundCallDispatcher]
MemoryOutboxWorkerFactory = Callable[[], PhoneMemoryOutboxWorker]
AiCallOutboxWorkerFactory = Callable[[], AiCallStartOutboxWorker]
PurgeWorkerFactory = Callable[[], PhoneDataPurgeWorker]
_RUNTIME_SHUTDOWN_GRACE_SECONDS = 5.0
_VOICE_CLIENT_CLOSE_GRACE_SECONDS = 2.0
_PURGE_STARTUP_GRACE_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TelephonyRuntimeStatus:
    enabled: bool = False
    ready: bool = False
    reason: str = "not_started"
    dispatcher_running: bool = False
    memory_outbox_running: bool = False
    ai_call_outbox_running: bool = False
    purge_worker_running: bool = False
    recovered_activations: int = 0


class _RecoveryOnlyRenderer:
    async def __call__(self, **_: Any) -> RenderedMp3:
        raise RuntimeError("startup recovery cannot render phone audio")


class TelephonyRuntime:
    """Own the one process dispatcher and its provider HTTP client."""

    def __init__(
        self,
        provider_runtime: TelephonyProviderRuntime,
        *,
        config_loader: ConfigLoader = load_telephony_config,
        readiness_probe: ReadinessProbe | None = None,
        elevenlabs_key_availability_probe: KeyAvailabilityProbe | None = None,
        elevenlabs_key_readiness_probe: KeyReadinessProbe | None = None,
        elevenlabs_key_prepare_probe: KeyPrepareProbe | None = None,
        stale_recovery: StaleRecovery | None = None,
        dispatcher_factory: DispatcherFactory = OutboundCallDispatcher,
        memory_outbox_worker_factory: MemoryOutboxWorkerFactory = (
            PhoneMemoryOutboxWorker
        ),
        ai_call_outbox_worker_factory: AiCallOutboxWorkerFactory | None = None,
        purge_worker_factory: PurgeWorkerFactory | None = None,
        ffmpeg_probe: FfmpegProbe = is_ffmpeg_available,
    ) -> None:
        self.provider_runtime = provider_runtime
        self._config_loader = config_loader
        self._readiness_probe = readiness_probe or self._probe_prerequisites
        self._elevenlabs_key_availability_probe = (
            elevenlabs_key_availability_probe or has_elevenlabs_keys
        )
        self._elevenlabs_key_readiness_probe = (
            elevenlabs_key_readiness_probe or elevenlabs_keys_ready
        )
        self._elevenlabs_key_prepare_probe = (
            elevenlabs_key_prepare_probe or prepare_elevenlabs_keys
        )
        self._elevenlabs_key_prepare_lock = asyncio.Lock()
        self._elevenlabs_key_prepare_task: asyncio.Task[bool] | None = None
        self._ffmpeg_probe = ffmpeg_probe
        self._stale_recovery = stale_recovery or self._recover_stale_activations
        self._dispatcher_factory = dispatcher_factory
        self._memory_outbox_worker_factory = memory_outbox_worker_factory
        self._ai_call_outbox_worker_factory = (
            ai_call_outbox_worker_factory or AiCallStartOutboxWorker
        )
        self._purge_worker_factory = (
            purge_worker_factory or create_phone_data_purge_worker
        )
        self._status = TelephonyRuntimeStatus()
        self._dispatcher: OutboundCallDispatcher | None = None
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._memory_outbox_worker: PhoneMemoryOutboxWorker | None = None
        self._memory_outbox_task: asyncio.Task[None] | None = None
        self._ai_call_outbox_worker: AiCallStartOutboxWorker | None = None
        self._ai_call_outbox_task: asyncio.Task[None] | None = None
        self._stop_event: asyncio.Event | None = None
        self._purge_worker: PhoneDataPurgeWorker | None = None
        self._purge_worker_task: asyncio.Task[None] | None = None
        self._purge_stop_event: asyncio.Event | None = None
        self._active_config: TelephonyConfig | None = None
        self._voice_client_closed = False
        self._stopping = False
        self._dispatch_admission_open = False
        self._stale_recovery_completed = False
        self._stale_recovery_result = 0
        self._lifecycle_lock = asyncio.Lock()
        self._worker_health_changed = asyncio.Event()
        # The registered routes consult this process owner, including after a
        # dynamic admin disable.  Per-call cache readiness remains a second gate.
        self.provider_runtime.readiness_check = self.is_ready

    def status(self) -> TelephonyRuntimeStatus:
        return replace(self._status)

    async def readiness_status(self) -> TelephonyRuntimeStatus:
        """Return readiness revalidated against live admin configuration."""

        if (
            not self._status.ready
            or not self._dispatch_admission_open
            or self._stopping
            or self._dispatcher_task is None
            or self._memory_outbox_task is None
            or self._ai_call_outbox_task is None
        ):
            return self.status()
        if not self._purge_worker_operational():
            return replace(
                self._status,
                ready=False,
                reason="purge_worker_unavailable",
                purge_worker_running=False,
            )
        workers = (
            ("dispatcher", self._dispatcher_task),
            ("memory_outbox", self._memory_outbox_task),
            ("ai_call_outbox", self._ai_call_outbox_task),
        )
        stopped_worker = next((name for name, task in workers if task.done()), None)
        if stopped_worker is not None:
            return replace(
                self._status,
                ready=False,
                reason=f"{stopped_worker}_stopped",
            )
        try:
            config = await self._config_loader()
            if not config.enabled:
                return replace(
                    self._status,
                    enabled=False,
                    ready=False,
                    reason="disabled",
                )
            reason = await self._probe_operational_readiness()
        except Exception:
            reason = "readiness_check_failed"
        if reason is not None:
            return replace(self._status, ready=False, reason=reason)
        if not self._purge_worker_operational():
            return replace(
                self._status,
                ready=False,
                reason="purge_worker_unavailable",
                purge_worker_running=False,
            )
        return self.status()

    async def is_ready(self) -> bool:
        return (await self.readiness_status()).ready

    async def start(self) -> TelephonyRuntimeStatus:
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def reconcile(self) -> TelephonyRuntimeStatus:
        """Apply the durable admin configuration to this process.

        Reconciliation is the only dynamic transition boundary.  It fences
        admission before replacing workers, is idempotent for an unchanged
        healthy configuration, and never starts a replacement while an old
        owned worker is still draining.
        """

        async with self._lifecycle_lock:
            self._dispatch_admission_open = False
            purge_failure = await self._ensure_purge_worker_locked()
            if purge_failure is not None:
                return await self._fail(
                    purge_failure,
                    enabled=self._status.enabled,
                )
            try:
                config = await self._config_loader()
            except Exception:
                self._status = replace(
                    self._status,
                    ready=False,
                    reason="configuration_unavailable",
                )
                return self.status()

            if not config.enabled:
                stopped = await self._stop_locked()
                self._active_config = None
                self._status = TelephonyRuntimeStatus(
                    enabled=False,
                    ready=False,
                    reason="disabled" if stopped else "disabled_draining",
                    dispatcher_running=self._task_running(self._dispatcher_task),
                    memory_outbox_running=self._task_running(
                        self._memory_outbox_task
                    ),
                    ai_call_outbox_running=self._task_running(
                        self._ai_call_outbox_task
                    ),
                    purge_worker_running=self._task_running(
                        self._purge_worker_task
                    ),
                    recovered_activations=self._status.recovered_activations,
                )
                return self.status()

            if (
                self._active_config == config
                and self._status.ready
                and self._task_running(self._dispatcher_task)
                and self._task_running(self._memory_outbox_task)
                and self._task_running(self._ai_call_outbox_task)
                and self._task_running(self._purge_worker_task)
            ):
                try:
                    reason = await self._probe_operational_readiness(
                        prepare_keys=True
                    )
                except Exception:
                    reason = "readiness_check_failed"
                if reason is not None:
                    return await self._fail(reason, enabled=True)
                self._dispatch_admission_open = True
                return self.status()

            if any(
                task is not None
                for task in (
                    self._dispatcher_task,
                    self._memory_outbox_task,
                    self._ai_call_outbox_task,
                )
            ):
                self._status = replace(
                    self._status,
                    ready=False,
                    reason="reconfiguring",
                )
                if not await self._stop_locked():
                    self._status = replace(
                        self._status,
                        enabled=True,
                        ready=False,
                        reason="reconfiguration_draining",
                    )
                    return self.status()
            return await self._start_locked(config=config)

    async def _start_locked(
        self,
        *,
        config: TelephonyConfig | None = None,
    ) -> TelephonyRuntimeStatus:
        purge_failure = await self._ensure_purge_worker_locked()
        if purge_failure is not None:
            return await self._fail(
                purge_failure,
                enabled=bool(config.enabled) if config is not None else False,
            )
        if self._dispatcher_task is not None and not self._dispatcher_task.done():
            return self.status()

        self._stopping = False
        self._dispatch_admission_open = False
        self._status = TelephonyRuntimeStatus(purge_worker_running=True)
        try:
            recovered = await self._recover_stale_once()
            self._status = replace(
                self._status,
                recovered_activations=recovered,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._fail(
                    "startup_recovery_canceled",
                    enabled=bool(config.enabled) if config is not None else False,
                )
            )
            raise
        except Exception as exc:
            logger.exception("Native telephone startup recovery failed")
            return await self._fail(
                f"startup_recovery_failed:{type(exc).__name__}",
                enabled=bool(config.enabled) if config is not None else False,
            )
        if config is None:
            try:
                config = await self._config_loader()
            except Exception:
                return await self._fail("configuration_unavailable")

        if not self._purge_worker_operational():
            return await self._fail(
                "purge_worker_stopped_during_startup",
                enabled=bool(config.enabled),
            )

        if not config.enabled:
            self._status = TelephonyRuntimeStatus(
                enabled=False,
                ready=False,
                reason="disabled",
                purge_worker_running=True,
                recovered_activations=recovered,
            )
            return self.status()

        self._voice_client_closed = False
        self._status = replace(self._status, enabled=True)
        try:
            reason = await self._probe_operational_readiness(
                prepare_keys=True
            )
            if reason is not None:
                return await self._fail(reason, enabled=True)
            if not self._purge_worker_operational():
                return await self._fail(
                    "purge_worker_stopped_during_startup",
                    enabled=True,
                )
            dispatcher = self._build_dispatcher(config)
            memory_outbox_worker = self._memory_outbox_worker_factory()
            ai_call_outbox_worker = self._ai_call_outbox_worker_factory()
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                dispatcher.run_until_stopped(stop_event),
                name="aurvek-phone-dispatcher",
            )
            memory_task = asyncio.create_task(
                memory_outbox_worker.run_until_stopped(stop_event),
                name="aurvek-phone-memory-outbox",
            )
            ai_call_task = asyncio.create_task(
                ai_call_outbox_worker.run_until_stopped(stop_event),
                name="aurvek-phone-ai-call-outbox",
            )
            self._dispatcher = dispatcher
            self._memory_outbox_worker = memory_outbox_worker
            self._ai_call_outbox_worker = ai_call_outbox_worker
            self._stop_event = stop_event
            self._dispatcher_task = task
            self._memory_outbox_task = memory_task
            self._ai_call_outbox_task = ai_call_task
            self._status = TelephonyRuntimeStatus(
                enabled=True,
                ready=False,
                reason="starting_workers",
                dispatcher_running=True,
                memory_outbox_running=True,
                ai_call_outbox_running=True,
                purge_worker_running=True,
                recovered_activations=recovered,
            )
            self._active_config = config
            task.add_done_callback(self._dispatcher_finished)
            memory_task.add_done_callback(self._memory_outbox_finished)
            ai_call_task.add_done_callback(self._ai_call_outbox_finished)
            # Give every owned task, including the purge supervisor, one turn
            # before publishing readiness. No provider admission opens until
            # this provisional cohort and the purge lease are revalidated.
            await asyncio.sleep(0)
            if not self._purge_worker_operational():
                failure_reason = self._status.reason
                if not failure_reason.startswith("purge_worker_"):
                    failure_reason = "purge_worker_stopped_during_startup"
                return await self._fail(failure_reason, enabled=True)
            stopped_worker = next(
                (
                    (name, owned)
                    for name, owned in (
                        ("dispatcher", task),
                        ("memory_outbox", memory_task),
                        ("ai_call_outbox", ai_call_task),
                    )
                    if owned.done()
                ),
                None,
            )
            if stopped_worker is not None:
                stopped_name, stopped_task = stopped_worker
                try:
                    stopped_task.result()
                except asyncio.CancelledError:
                    failure_reason = self._status.reason
                    if failure_reason == "starting_workers":
                        failure_reason = f"{stopped_name}_stopped_during_startup"
                except Exception as exc:
                    failure_reason = f"startup_failed:{type(exc).__name__}"
                else:
                    failure_reason = f"{stopped_name}_stopped_during_startup"
                return await self._fail(failure_reason, enabled=True)
            self._status = replace(
                self._status,
                ready=True,
                reason="ready",
            )
            self._dispatch_admission_open = True
        except asyncio.CancelledError:
            await asyncio.shield(self._fail("startup_canceled", enabled=True))
            raise
        except Exception as exc:
            logger.exception("Native telephone runtime failed to start")
            return await self._fail(
                f"startup_failed:{type(exc).__name__}", enabled=True
            )

        logger.info("Native telephone runtime is ready")
        return self.status()

    async def _ensure_purge_worker_locked(self) -> str | None:
        task = self._purge_worker_task
        if task is not None and not task.done():
            if self._purge_worker_operational():
                self._status = replace(self._status, purge_worker_running=True)
                return None
            self._status = replace(
                self._status,
                ready=False,
                reason="purge_worker_cleanup_draining",
                purge_worker_running=True,
            )
            return "purge_worker_cleanup_draining"

        if task is not None and task.done():
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            self._purge_worker = None
            self._purge_worker_task = None
            self._purge_stop_event = None

        try:
            worker = self._purge_worker_factory()
            stop_event = asyncio.Event()
            task = asyncio.create_task(
                worker.run_until_stopped(stop_event),
                name="aurvek-phone-data-purge",
            )
            self._purge_worker = worker
            self._purge_worker_task = task
            self._purge_stop_event = stop_event
            await asyncio.wait_for(
                self._wait_for_purge_lease(worker, task),
                timeout=_PURGE_STARTUP_GRACE_SECONDS,
            )
            task.add_done_callback(self._purge_worker_finished)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_purge_locked())
            raise
        except Exception as exc:
            logger.exception("Telephone data purge worker failed to start")
            await self._stop_purge_locked()
            return f"purge_worker_startup_failed:{type(exc).__name__}"

        self._status = replace(self._status, purge_worker_running=True)
        return None

    def _purge_worker_operational(self) -> bool:
        task = self._purge_worker_task
        worker = self._purge_worker
        return bool(
            task is not None
            and not task.done()
            and worker is not None
            and getattr(worker, "_runtime_token", None) is not None
        )

    @staticmethod
    async def _wait_for_purge_lease(
        worker: PhoneDataPurgeWorker,
        task: asyncio.Task[None],
    ) -> None:
        """Wait boundedly until the worker exposes its acquired runtime lease."""

        while getattr(worker, "_runtime_token", None) is None:
            if task.done():
                task.result()
                raise RuntimeError("phone data purge worker stopped during startup")
            await asyncio.sleep(0)
        if task.done():
            task.result()

    async def _recover_stale_once(self) -> int:
        """Memoize one successful startup recovery for this runtime instance."""

        if self._stale_recovery_completed:
            return self._stale_recovery_result
        recovered = int(await self._stale_recovery())
        self._stale_recovery_result = recovered
        self._stale_recovery_completed = True
        return recovered

    async def stop(self) -> None:
        """Quiesce workers and close the shared provider client for final shutdown."""

        async with self._lifecycle_lock:
            conversational_stopped = await self._stop_locked()
            await self._stop_purge_locked()
            if not conversational_stopped:
                self._status = replace(self._status, reason="stop_incomplete")
            await self._close_voice_client_bounded()

    async def _stop_locked(self) -> bool:
        """Quiesce owned workers while leaving the shared provider client usable."""

        self._stopping = True
        self._dispatch_admission_open = False
        self._status = replace(self._status, ready=False, reason="stopping")
        loop = asyncio.get_running_loop()
        graceful_deadline = loop.time() + (_RUNTIME_SHUTDOWN_GRACE_SECONDS / 2)
        task = self._dispatcher_task
        memory_task = self._memory_outbox_task
        ai_call_task = self._ai_call_outbox_task
        stop_event = self._stop_event
        survivors: tuple[asyncio.Task[None], ...] = ()
        try:
            if stop_event is not None:
                stop_event.set()
            owned_tasks = [
                owned
                for owned in (task, memory_task, ai_call_task)
                if owned is not None
            ]
            if owned_tasks:
                survivors = await cancel_and_join_tasks(
                    owned_tasks,
                    deadline=graceful_deadline,
                    cancel_first=False,
                )
                if survivors:
                    survivors = await cancel_and_join_tasks(
                        survivors,
                        deadline=(
                            loop.time() + (_RUNTIME_SHUTDOWN_GRACE_SECONDS / 2)
                        ),
                    )
        finally:
            dispatcher_running = self._task_running(task)
            memory_running = self._task_running(memory_task)
            ai_call_running = self._task_running(ai_call_task)
            if dispatcher_running or memory_running or ai_call_running:
                logger.error(
                    "Telephone runtime retained ownership of draining workers: %s",
                    ",".join(
                        owned.get_name()
                        for owned in (task, memory_task, ai_call_task)
                        if self._task_running(owned)
                    ),
                )
            if not dispatcher_running:
                self._dispatcher = None
                self._dispatcher_task = None
            if not memory_running:
                self._memory_outbox_worker = None
                self._memory_outbox_task = None
            if not ai_call_running:
                self._ai_call_outbox_worker = None
                self._ai_call_outbox_task = None
            if not dispatcher_running and not memory_running and not ai_call_running:
                self._stop_event = None
                self._active_config = None
            self._status = replace(
                self._status,
                ready=False,
                reason=(
                    "stop_incomplete"
                    if dispatcher_running or memory_running or ai_call_running
                    else "stopped"
                ),
                dispatcher_running=dispatcher_running,
                memory_outbox_running=memory_running,
                ai_call_outbox_running=ai_call_running,
            )
        return not any(
            self._task_running(owned)
            for owned in (
                self._dispatcher_task,
                self._memory_outbox_task,
                self._ai_call_outbox_task,
            )
        )

    async def _stop_purge_locked(self) -> bool:
        task = self._purge_worker_task
        stop_event = self._purge_stop_event
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            survivors = await cancel_and_join_tasks(
                (task,),
                deadline=(
                    asyncio.get_running_loop().time()
                    + _RUNTIME_SHUTDOWN_GRACE_SECONDS
                ),
                cancel_first=False,
            )
        else:
            survivors = ()
        running = bool(survivors) and self._task_running(task)
        if not running:
            self._purge_worker = None
            self._purge_worker_task = None
            self._purge_stop_event = None
        self._status = replace(
            self._status,
            ready=False,
            purge_worker_running=running,
            reason=(
                self._status.reason
                if self._status.reason == "stop_incomplete"
                else (
                    "maintenance_stop_incomplete" if running else "stopped"
                )
            ),
        )
        return not running

    def _build_dispatcher(self, config: TelephonyConfig) -> OutboundCallDispatcher:
        client = self.provider_runtime.voice_client
        if client is None:
            raise RuntimeError("Twilio Voice client is unavailable")
        repository = TelephonyRepository()

        def twiml_url(token: str) -> str:
            return canonical_twilio_url(VOICE_TWIML_PATH.format(token=token))

        def status_url(token: str) -> str:
            return canonical_twilio_url(VOICE_STATUS_PATH.format(token=token))

        def amd_url(token: str) -> str:
            return canonical_twilio_url(VOICE_AMD_PATH.format(token=token))

        identity = f"phone-dispatcher-{socket.gethostname()}-{os.getpid()}"
        return self._dispatcher_factory(
            repository,
            client,
            dispatcher_id=identity,
            twiml_url_factory=twiml_url,
            status_callback_url_factory=status_url,
            amd_status_callback_url_factory=amd_url,
            jitter_seconds=config.scheduler_jitter_seconds,
            max_concurrent_dispatches=config.max_concurrent_dispatches,
            config_loader=self._load_dispatch_config,
            dispatch_fence=self._dispatch_fence,
        )

    async def _dispatch_fence(
        self,
        dispatcher_id: str,
        call: dict[str, Any] | None = None,
    ) -> bool:
        """Revalidate the exact live dispatcher immediately before a POST."""

        if not self._dispatch_owner_matches(dispatcher_id):
            return False
        try:
            config = await self._config_loader()
            if (
                not config.enabled
                or await self._probe_operational_readiness() is not None
            ):
                return False
            if call is not None and not await self.provider_runtime.call_ready(call):
                return False
        except Exception:
            return False
        return self._dispatch_owner_matches(dispatcher_id)

    def _dispatch_owner_matches(self, dispatcher_id: str) -> bool:
        dispatcher = self._dispatcher
        dispatcher_task = self._dispatcher_task
        memory_task = self._memory_outbox_task
        ai_call_task = self._ai_call_outbox_task
        return bool(
            self._dispatch_admission_open
            and not self._stopping
            and self._status.ready
            and self._status.dispatcher_running
            and self._status.memory_outbox_running
            and self._status.ai_call_outbox_running
            and self._status.purge_worker_running
            and dispatcher is not None
            and getattr(dispatcher, "dispatcher_id", None) == dispatcher_id
            and dispatcher_task is not None
            and not dispatcher_task.done()
            and memory_task is not None
            and not memory_task.done()
            and ai_call_task is not None
            and not ai_call_task.done()
            and self._purge_worker_operational()
        )

    async def _load_dispatch_config(self) -> TelephonyConfig:
        """Fence each due provider POST with the same operational readiness."""

        memory_task = self._memory_outbox_task
        ai_call_task = self._ai_call_outbox_task
        if (
            memory_task is None
            or memory_task.done()
            or not self._status.memory_outbox_running
        ):
            raise RuntimeError("telephone memory outbox is not running")
        if (
            ai_call_task is None
            or ai_call_task.done()
            or not self._status.ai_call_outbox_running
        ):
            raise RuntimeError("telephone AI call outbox is not running")
        if (
            not self._status.purge_worker_running
            or not self._purge_worker_operational()
        ):
            raise RuntimeError("telephone data purge worker is not running")
        if (
            not self._dispatch_admission_open
            or self._stopping
            or not self._status.ready
            or not self._status.dispatcher_running
        ):
            raise RuntimeError("telephone runtime workers are not running")
        config = await self._config_loader()
        if not config.enabled:
            raise RuntimeError("telephone runtime is disabled")
        reason = await self._probe_operational_readiness(prepare_keys=True)
        if reason is not None:
            raise RuntimeError(f"telephone runtime is not ready: {reason}")
        if not self._purge_worker_operational():
            raise RuntimeError("telephone data purge worker is not running")
        return config

    async def _probe_operational_readiness(
        self,
        *,
        prepare_keys: bool = False,
    ) -> str | None:
        """Check route-neutral readiness and opportunistically warm Scribe."""

        active = self.provider_runtime
        if prepare_keys:
            try:
                if (
                    callable(active.elevenlabs_api_key_provider)
                    and self._elevenlabs_key_availability_probe()
                ):
                    await self._prepare_elevenlabs_key_pool()
            except Exception:
                # A Realtime-only call must remain dispatchable when Scribe is
                # absent or temporarily unready. Standard calls are fenced by
                # TelephonyProviderRuntime.call_ready against cached readiness.
                pass
        return await self._readiness_probe()

    async def _prepare_elevenlabs_key_pool(self) -> bool:
        """Coalesce concurrent refreshes before entering the worker pool."""

        async with self._elevenlabs_key_prepare_lock:
            task = self._elevenlabs_key_prepare_task
            if task is None or task.done():
                task = asyncio.create_task(
                    asyncio.to_thread(self._elevenlabs_key_prepare_probe),
                    name="aurvek-elevenlabs-key-pool-prepare",
                )
                self._elevenlabs_key_prepare_task = task
        try:
            return bool(await asyncio.shield(task))
        finally:
            if task.done():
                async with self._elevenlabs_key_prepare_lock:
                    if self._elevenlabs_key_prepare_task is task:
                        self._elevenlabs_key_prepare_task = None

    async def _probe_prerequisites(self) -> str | None:
        active = self.provider_runtime
        if not active.account_sid or active.signature_verifier() is None:
            return "twilio_credentials_missing"
        if active.voice_client is None:
            return "twilio_client_unavailable"
        try:
            if not await self._ffmpeg_probe():
                return "ffmpeg_unavailable"
        except Exception:
            return "ffmpeg_unavailable"
        if (
            active.notice_loader is None
            or active.greeting_loader is None
            or active.context_readiness is None
            or active.unknown_notice_loader is None
        ):
            return "audio_cache_backend_unavailable"

        try:
            canonical_twilio_url(VOICE_TWIML_PATH.format(token="probe"))
            canonical_twilio_url("/ws/twilio/media-stream", websocket=True)
        except Exception:
            return "canonical_public_domain_missing"

        try:
            async with get_db_connection(readonly=True) as conn:
                voice_cursor = await conn.execute(
                    "SELECT COUNT(*) FROM VOICES WHERE is_default=1 "
                    "AND COALESCE(deprecated,0)=0"
                )
                number_cursor = await conn.execute(
                    "SELECT COUNT(*) FROM TELEPHONY_NUMBERS "
                    "WHERE enabled=1 AND is_outbound_default=1"
                )
                default_voices = int((await voice_cursor.fetchone())[0])
                default_numbers = int((await number_cursor.fetchone())[0])
                billing = await phone_billing_readiness(conn)
        except Exception:
            return "telephony_database_unavailable"
        if default_voices != 1:
            return "default_voice_missing"
        if default_numbers != 1:
            return "default_phone_number_missing"
        if not billing["ready"]:
            return "phone_billing_rates_missing"
        try:
            await active.unknown_notice_loader()
        except Exception:
            return "global_audio_cache_unavailable"
        return None

    async def _recover_stale_activations(self) -> int:
        billing = await recover_stale_phone_billing()
        if billing["refunded"] or billing["needs_attention"]:
            logger.warning(
                "Recovered stale telephone billing holds: refunded=%d attention=%d",
                billing["refunded"],
                billing["needs_attention"],
            )
        service = PhoneAudioCacheService(renderer=_RecoveryOnlyRenderer())
        async with get_db_connection() as conn:
            result = await service.recover_stale_activations(
                conn,
                created_before=datetime.now(UTC) - timedelta(hours=1),
                protected_activation_ids=(),
            )
        return len(result.canceled_activation_ids)

    def _dispatcher_finished(self, task: asyncio.Task[None]) -> None:
        if task is not self._dispatcher_task or self._stopping:
            return
        if task.cancelled():
            was_ready = self._status.ready
            self._dispatch_admission_open = False
            self._status = replace(
                self._status,
                ready=False,
                reason=(
                    "dispatcher_cancelled" if was_ready else self._status.reason
                ),
                dispatcher_running=False,
            )
            self._worker_health_changed.set()
            return
        try:
            task.result()
            reason = "dispatcher_stopped"
        except Exception as exc:
            logger.exception(
                "Native telephone dispatcher stopped unexpectedly",
                exc_info=exc,
            )
            reason = f"dispatcher_failed:{type(exc).__name__}"
        self._dispatch_admission_open = False
        self._status = replace(
            self._status,
            ready=False,
            reason=reason,
            dispatcher_running=False,
        )
        self._worker_health_changed.set()

    def _memory_outbox_finished(self, task: asyncio.Task[None]) -> None:
        if task is not self._memory_outbox_task or self._stopping:
            return
        if task.cancelled():
            was_ready = self._status.ready
            self._dispatch_admission_open = False
            self._status = replace(
                self._status,
                ready=False,
                reason=(
                    "memory_outbox_cancelled" if was_ready else self._status.reason
                ),
                memory_outbox_running=False,
            )
            self._worker_health_changed.set()
            return
        try:
            task.result()
            reason = "memory_outbox_stopped"
        except Exception as exc:
            logger.exception(
                "Native telephone memory outbox stopped unexpectedly",
                exc_info=exc,
            )
            reason = f"memory_outbox_failed:{type(exc).__name__}"
        self._dispatch_admission_open = False
        self._status = replace(
            self._status,
            ready=False,
            reason=reason,
            memory_outbox_running=False,
        )
        self._worker_health_changed.set()
        dispatcher_task = self._dispatcher_task
        if dispatcher_task is not None and not dispatcher_task.done():
            dispatcher_task.cancel()

    def _ai_call_outbox_finished(self, task: asyncio.Task[None]) -> None:
        if task is not self._ai_call_outbox_task or self._stopping:
            return
        if task.cancelled():
            reason = (
                "ai_call_outbox_cancelled"
                if self._status.ready
                else self._status.reason
            )
        else:
            try:
                task.result()
                reason = "ai_call_outbox_stopped"
            except Exception as exc:
                logger.exception(
                    "Native telephone AI call outbox stopped unexpectedly",
                    exc_info=exc,
                )
                reason = f"ai_call_outbox_failed:{type(exc).__name__}"
        self._dispatch_admission_open = False
        self._status = replace(
            self._status,
            ready=False,
            reason=reason,
            ai_call_outbox_running=False,
        )
        self._worker_health_changed.set()
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        for sibling in (self._dispatcher_task, self._memory_outbox_task):
            if sibling is not None and not sibling.done():
                sibling.cancel()

    def _purge_worker_finished(self, task: asyncio.Task[None]) -> None:
        if task is not self._purge_worker_task or task.cancelled():
            return
        try:
            task.result()
            reason = "purge_worker_stopped"
        except Exception as exc:
            logger.exception(
                "Telephone data purge worker stopped unexpectedly",
                exc_info=exc,
            )
            reason = f"purge_worker_failed:{type(exc).__name__}"
        self._dispatch_admission_open = False
        self._status = replace(
            self._status,
            ready=False,
            reason=reason,
            purge_worker_running=False,
        )
        self._worker_health_changed.set()
        stop_event = self._stop_event
        if stop_event is not None:
            stop_event.set()
        for owned in (
            self._dispatcher_task,
            self._memory_outbox_task,
            self._ai_call_outbox_task,
        ):
            if owned is not None and not owned.done():
                owned.cancel()

    async def _fail(
        self, reason: str, *, enabled: bool = False
    ) -> TelephonyRuntimeStatus:
        self._dispatch_admission_open = False
        stopped = await self._stop_locked()
        self._status = TelephonyRuntimeStatus(
            enabled=enabled,
            ready=False,
            reason=reason if stopped else f"{reason}:cleanup_draining",
            dispatcher_running=self._task_running(self._dispatcher_task),
            memory_outbox_running=self._task_running(self._memory_outbox_task),
            ai_call_outbox_running=self._task_running(self._ai_call_outbox_task),
            purge_worker_running=self._task_running(self._purge_worker_task),
            recovered_activations=self._status.recovered_activations,
        )
        return self.status()

    async def _close_voice_client(self) -> None:
        client = self.provider_runtime.voice_client
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            logger.exception("Error closing native telephone provider client")

    async def _close_voice_client_bounded(self) -> None:
        if self._voice_client_closed:
            return
        close_task = asyncio.create_task(
            self._close_voice_client(),
            name="aurvek-phone-voice-client-close",
        )
        survivors = await cancel_and_join_tasks(
            (close_task,),
            deadline=(
                asyncio.get_running_loop().time()
                + _VOICE_CLIENT_CLOSE_GRACE_SECONDS
            ),
            cancel_first=False,
        )
        if survivors:
            logger.error("Telephone provider client close exceeded grace")
        else:
            self._voice_client_closed = True

    @staticmethod
    def _task_running(task: asyncio.Task[Any] | None) -> bool:
        return task is not None and not task.done()


_runtime: TelephonyRuntime | None = None


def get_telephony_runtime() -> TelephonyRuntime:
    global _runtime
    if _runtime is None:
        _runtime = TelephonyRuntime(get_provider_runtime())
    return _runtime


__all__ = [
    "TelephonyRuntime",
    "TelephonyRuntimeStatus",
    "get_telephony_runtime",
]
