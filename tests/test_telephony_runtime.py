from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

import common
from integrations import routes as integration_routes
from integrations.telephony import runtime as runtime_module
from integrations.telephony.config import TelephonyConfig
from integrations.telephony.routes import TelephonyProviderRuntime
from integrations.telephony.runtime import TelephonyRuntime


class _VoiceClient:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


class _Dispatcher:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.dispatcher_id = kwargs.get("dispatcher_id")
        self.started = asyncio.Event()
        self.stop_event: asyncio.Event | None = None

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self.started.set()
        await stop_event.wait()


class _PurgeWorker(_Dispatcher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._runtime_token: str | None = None
        self.finished = asyncio.Event()

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self._runtime_token = "purge-runtime-lease"
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self._runtime_token = None
            self.finished.set()


class _FailingPurgeWorker(_PurgeWorker):
    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        del stop_event
        raise RuntimeError("purge lease unavailable")


class _CrashingPurgeWorker(_PurgeWorker):
    def __init__(self) -> None:
        super().__init__()
        self.crash = asyncio.Event()

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        self.stop_event = stop_event
        self._runtime_token = "purge-runtime-lease"
        self.started.set()
        await self.crash.wait()
        self._runtime_token = None
        raise RuntimeError("purge heartbeat failed")


@pytest.fixture(autouse=True)
def _inject_default_lifecycle_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "AiCallStartOutboxWorker", _Dispatcher)
    monkeypatch.setattr(
        runtime_module,
        "create_phone_data_purge_worker",
        _PurgeWorker,
    )
    monkeypatch.setattr(runtime_module, "has_elevenlabs_keys", lambda: True)
    monkeypatch.setattr(
        runtime_module,
        "prepare_elevenlabs_keys",
        lambda: True,
    )
    monkeypatch.setattr(
        runtime_module,
        "elevenlabs_keys_ready",
        lambda: True,
    )


class _FailingDispatcher(_Dispatcher):
    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        del stop_event
        raise RuntimeError("dispatcher failed")


class _ObservedDispatcher(_Dispatcher):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finished = asyncio.Event()

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        self.started.set()
        try:
            await stop_event.wait()
        finally:
            self.finished.set()


class _CrashingOutboxWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.crash = asyncio.Event()

    async def run_until_stopped(self, _stop_event: asyncio.Event) -> None:
        self.started.set()
        await self.crash.wait()
        raise RuntimeError("outbox crashed")


class _ShutdownFailingOutboxWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()

    async def run_until_stopped(self, stop_event: asyncio.Event) -> None:
        self.started.set()
        await stop_event.wait()
        self.finished.set()
        raise RuntimeError("outbox shutdown failed")


class _DelayedCancellationDispatcher(_Dispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_started = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.owned_task: asyncio.Task[None] | None = None

    async def run_until_stopped(self, _stop_event: asyncio.Event) -> None:
        self.owned_task = asyncio.current_task()
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancel_started.set()
            while not self.release_cleanup.is_set():
                try:
                    await self.release_cleanup.wait()
                except asyncio.CancelledError:
                    continue
            raise


class _DelayedCancellationPurgeWorker(_DelayedCancellationDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self._runtime_token: str | None = None


async def _unused_loader(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    return object()


def _provider(voice_client: _VoiceClient) -> TelephonyProviderRuntime:
    return TelephonyProviderRuntime(
        account_sid="AC" + "1" * 32,
        auth_token="test-auth-token",
        elevenlabs_api_key_provider=lambda: "elevenlabs-key",
        repository=object(),  # type: ignore[arg-type]
        readiness_check=lambda: asyncio.sleep(0, result=True),
        notice_loader=_unused_loader,
        greeting_loader=_unused_loader,
        context_readiness=lambda context: asyncio.sleep(0, result=True),
        unknown_notice_loader=_unused_loader,
        voice_client=voice_client,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_missing_scribe_capacity_does_not_block_route_neutral_runtime() -> None:
    voice = _VoiceClient()
    provider = _provider(voice)
    key_provider_calls = 0
    workers: list[_Dispatcher] = []

    def key_provider() -> str:
        nonlocal key_provider_calls
        key_provider_calls += 1
        return "unavailable-key"

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        workers.append(instance)
        return instance

    provider.elevenlabs_api_key_provider = key_provider
    runtime = TelephonyRuntime(
        provider,
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_availability_probe=lambda: False,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.ready
    assert status.dispatcher_running
    assert runtime._dispatch_admission_open
    assert len(workers) == 1
    assert key_provider_calls == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_dispatch_fence_applies_call_specific_provider_readiness(
    monkeypatch,
) -> None:
    voice = _VoiceClient()
    dispatchers: list[_Dispatcher] = []
    checked = []

    async def call_ready(_self, call):
        checked.append(call)
        return call["runtime_kind"] == "openai_realtime"

    monkeypatch.setattr(TelephonyProviderRuntime, "call_ready", call_ready)

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(
            0, result=TelephonyConfig(enabled=True)
        ),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_availability_probe=lambda: False,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    dispatcher_id = dispatchers[0].dispatcher_id

    assert not await runtime._dispatch_fence(
        dispatcher_id, {"runtime_kind": "standard"}
    )
    assert await runtime._dispatch_fence(
        dispatcher_id, {"runtime_kind": "openai_realtime"}
    )
    assert [item["runtime_kind"] for item in checked] == [
        "standard",
        "openai_realtime",
    ]
    await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_prepares_key_pool_off_loop_before_dispatch_admission() -> None:
    voice = _VoiceClient()
    entered = threading.Event()
    release = threading.Event()
    probe_threads: list[int] = []
    dispatchers: list[_Dispatcher] = []
    event_loop_thread = threading.get_ident()

    def prepare_keys() -> bool:
        probe_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(timeout=2)
        return True

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_readiness_probe=lambda: True,
        elevenlabs_key_prepare_probe=prepare_keys,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    start_task = asyncio.create_task(runtime.start())
    assert await asyncio.to_thread(entered.wait, 1)
    assert not start_task.done()
    assert dispatchers == []
    assert not runtime._dispatch_admission_open

    release.set()
    status = await start_task

    assert status.ready
    assert len(probe_threads) == 1
    assert probe_threads[0] != event_loop_thread
    assert len(dispatchers) == 1
    assert runtime._dispatch_admission_open
    await runtime.stop()


@pytest.mark.asyncio
async def test_unready_scribe_warmup_does_not_block_realtime_runtime() -> None:
    voice = _VoiceClient()
    dispatchers: list[_Dispatcher] = []

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_readiness_probe=lambda: False,
        elevenlabs_key_prepare_probe=lambda: False,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.ready
    assert status.dispatcher_running
    assert len(dispatchers) == 1
    assert runtime._dispatch_admission_open
    await runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_early_preflights_serialize_key_pool_preparation() -> None:
    voice = _VoiceClient()
    active = 0
    maximum_active = 0
    calls = 0
    counter_lock = threading.Lock()

    def prepare_keys() -> bool:
        nonlocal active, maximum_active, calls
        with counter_lock:
            active += 1
            calls += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.01)
        with counter_lock:
            active -= 1
        return True

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_readiness_probe=lambda: True,
        elevenlabs_key_prepare_probe=lambda: True,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=_Dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    runtime._elevenlabs_key_prepare_probe = prepare_keys

    configs = await asyncio.gather(
        *(runtime._load_dispatch_config() for _ in range(6))
    )

    assert all(config.enabled for config in configs)
    assert calls == 1
    assert maximum_active == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_final_fence_and_status_are_route_neutral_and_do_not_prepare_keys() -> None:
    voice = _VoiceClient()
    dispatcher = _Dispatcher()
    prepare_calls = 0
    cached_calls = 0

    def prepare_keys() -> bool:
        nonlocal prepare_calls
        prepare_calls += 1
        return True

    def cached_ready() -> bool:
        nonlocal cached_calls
        cached_calls += 1
        return True

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_readiness_probe=cached_ready,
        elevenlabs_key_prepare_probe=prepare_keys,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=lambda *_args, **_kwargs: dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    assert prepare_calls == 1

    assert (await runtime.readiness_status()).ready
    assert await runtime._dispatch_fence(dispatcher.dispatcher_id)

    assert prepare_calls == 1
    assert cached_calls == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_scribe_capacity_loss_does_not_fence_realtime_runtime() -> None:
    voice = _VoiceClient()
    key_available = True
    dispatchers: list[_Dispatcher] = []

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        elevenlabs_key_availability_probe=lambda: key_available,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    dispatcher_id = dispatchers[0].dispatcher_id

    key_available = False

    readiness = await runtime.readiness_status()
    assert readiness.ready
    assert await dispatchers[0].kwargs["dispatch_fence"](dispatcher_id)
    assert (await dispatchers[0].kwargs["config_loader"]()).enabled

    reconciled = await runtime.reconcile()
    assert reconciled.ready
    assert runtime._dispatch_admission_open
    await runtime.stop()


@pytest.mark.asyncio
async def test_disabled_runtime_recovers_without_starting_dispatcher() -> None:
    voice = _VoiceClient()
    calls = {"recovery": 0, "dispatcher": 0}

    async def recover() -> int:
        calls["recovery"] += 1
        return 3

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        del args, kwargs
        calls["dispatcher"] += 1
        return _Dispatcher()

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=False)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=recover,
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.reason == "disabled"
    assert not status.enabled
    assert not status.ready
    assert status.recovered_activations == 3
    assert calls == {"recovery": 1, "dispatcher": 0}
    assert voice.closed == 0
    await runtime.stop()
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_disabled_runtime_starts_only_purge_maintenance_worker() -> None:
    voice = _VoiceClient()
    purge = _PurgeWorker()
    ai_workers: list[_Dispatcher] = []

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=False)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=_Dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=lambda: ai_workers.append(_Dispatcher())
        or ai_workers[-1],  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.reason == "disabled"
    assert status.purge_worker_running
    assert not status.dispatcher_running
    assert not status.memory_outbox_running
    assert not status.ai_call_outbox_running
    assert purge.started.is_set()
    assert ai_workers == []
    await runtime.stop()
    assert purge.finished.is_set()


@pytest.mark.asyncio
async def test_purge_startup_failure_is_fail_closed_before_all_other_workers() -> None:
    voice = _VoiceClient()
    created = {"dispatcher": 0, "memory": 0, "ai": 0}

    def worker(kind: str):
        def create(*_args: Any, **_kwargs: Any) -> _Dispatcher:
            created[kind] += 1
            return _Dispatcher()

        return create

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=worker("dispatcher"),
        memory_outbox_worker_factory=worker("memory"),  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=worker("ai"),  # type: ignore[arg-type]
        purge_worker_factory=_FailingPurgeWorker,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.reason == "purge_worker_startup_failed:RuntimeError"
    assert not status.ready
    assert not status.purge_worker_running
    assert created == {"dispatcher": 0, "memory": 0, "ai": 0}
    await runtime.stop()


@pytest.mark.asyncio
async def test_purge_death_during_startup_cannot_be_overwritten_by_ready_state() -> None:
    voice = _VoiceClient()
    purge = _CrashingPurgeWorker()
    recovery_entered = asyncio.Event()
    release_recovery = asyncio.Event()
    created = {"dispatcher": 0, "memory": 0, "ai": 0}

    async def stale_recovery() -> int:
        recovery_entered.set()
        await release_recovery.wait()
        return 0

    def worker(kind: str):
        def create(*_args: Any, **_kwargs: Any) -> _Dispatcher:
            created[kind] += 1
            return _Dispatcher()

        return create

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=stale_recovery,
        dispatcher_factory=worker("dispatcher"),
        memory_outbox_worker_factory=worker("memory"),  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=worker("ai"),  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )

    starting = asyncio.create_task(runtime.start())
    await asyncio.wait_for(recovery_entered.wait(), timeout=0.5)
    purge.crash.set()
    await asyncio.wait_for(runtime._worker_health_changed.wait(), timeout=0.5)
    release_recovery.set()
    status = await asyncio.wait_for(starting, timeout=0.5)

    assert status.reason == "purge_worker_stopped_during_startup"
    assert not status.ready
    assert not status.purge_worker_running
    assert created == {"dispatcher": 0, "memory": 0, "ai": 0}
    await runtime.stop()


@pytest.mark.asyncio
async def test_purge_death_triggered_by_dispatcher_factory_never_opens_admission() -> None:
    voice = _VoiceClient()
    purge = _CrashingPurgeWorker()

    def dispatcher(*args: Any, **kwargs: Any) -> _ObservedDispatcher:
        purge.crash.set()
        return _ObservedDispatcher(*args, **kwargs)

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_ObservedDispatcher,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=_ObservedDispatcher,  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert not status.ready
    assert status.reason.startswith("purge_worker_")
    assert not runtime._dispatch_admission_open
    assert runtime._dispatcher_task is None
    assert runtime._memory_outbox_task is None
    assert runtime._ai_call_outbox_task is None
    await runtime.stop()


@pytest.mark.asyncio
async def test_purge_startup_timeout_retains_owned_task_and_blocks_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_module, "_PURGE_STARTUP_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(runtime_module, "_RUNTIME_SHUTDOWN_GRACE_SECONDS", 0.02)
    voice = _VoiceClient()
    delayed = _DelayedCancellationPurgeWorker()
    created: list[_Dispatcher] = []

    def purge_factory() -> _Dispatcher:
        worker = delayed if not created else _PurgeWorker()
        created.append(worker)
        return worker

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=False)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=_Dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        purge_worker_factory=purge_factory,  # type: ignore[arg-type]
    )

    first = await runtime.start()
    owned_task = runtime._purge_worker_task
    try:
        assert first.reason == "purge_worker_startup_failed:TimeoutError"
        assert delayed.cancel_started.is_set()
        assert owned_task is not None and not owned_task.done()

        retried = await runtime.reconcile()

        assert retried.reason == "purge_worker_cleanup_draining"
        assert runtime._purge_worker_task is owned_task
        assert len(created) == 1
    finally:
        delayed.release_cleanup.set()
    for _ in range(50):
        if owned_task.done():
            break
        await asyncio.sleep(0)
    assert owned_task.done()
    await runtime.stop()


@pytest.mark.parametrize(
    ("enabled", "readiness_reason", "expected_reason"),
    (
        (False, None, "disabled"),
        (True, "phone_billing_rates_missing", "phone_billing_rates_missing"),
    ),
)
@pytest.mark.asyncio
async def test_successful_recovery_is_memoized_for_concurrent_nonoperational_starts(
    enabled: bool,
    readiness_reason: str | None,
    expected_reason: str,
) -> None:
    voice = _VoiceClient()
    recovery_entered = asyncio.Event()
    release_recovery = asyncio.Event()
    calls = {"recovery": 0, "dispatcher": 0, "outbox": 0}

    async def recover() -> int:
        calls["recovery"] += 1
        recovery_entered.set()
        await release_recovery.wait()
        return 4

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        del args, kwargs
        calls["dispatcher"] += 1
        return _Dispatcher()

    def outbox() -> _Dispatcher:
        calls["outbox"] += 1
        return _Dispatcher()

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(
            0, result=TelephonyConfig(enabled=enabled)
        ),
        readiness_probe=lambda: asyncio.sleep(0, result=readiness_reason),
        stale_recovery=recover,
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=outbox,  # type: ignore[arg-type]
    )

    first = asyncio.create_task(runtime.start())
    await recovery_entered.wait()
    second = asyncio.create_task(runtime.start())
    release_recovery.set()
    statuses = await asyncio.gather(first, second)

    assert [status.reason for status in statuses] == [
        expected_reason,
        expected_reason,
    ]
    assert all(status.recovered_activations == 4 for status in statuses)
    assert calls == {"recovery": 1, "dispatcher": 0, "outbox": 0}
    await runtime.stop()


@pytest.mark.asyncio
async def test_failed_recovery_retries_without_starting_workers() -> None:
    voice = _VoiceClient()
    calls = {"recovery": 0, "dispatcher": 0, "outbox": 0}

    async def recover() -> int:
        calls["recovery"] += 1
        if calls["recovery"] == 1:
            raise RuntimeError("temporary recovery failure")
        return 6

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        calls["dispatcher"] += 1
        return _Dispatcher(*args, **kwargs)

    def outbox() -> _Dispatcher:
        calls["outbox"] += 1
        return _Dispatcher()

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=recover,
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=outbox,  # type: ignore[arg-type]
    )

    failed = await runtime.start()
    assert failed.reason == "startup_recovery_failed:RuntimeError"
    assert calls == {"recovery": 1, "dispatcher": 0, "outbox": 0}

    recovered = await runtime.start()
    assert recovered.ready
    assert recovered.recovered_activations == 6
    assert calls == {"recovery": 2, "dispatcher": 1, "outbox": 1}
    await runtime.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_starts_one_dispatcher_with_canonical_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "phone.example.com")
    voice = _VoiceClient()
    created: list[_Dispatcher] = []
    memory_workers: list[_Dispatcher] = []

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        created.append(instance)
        return instance

    def memory_worker() -> _Dispatcher:
        instance = _Dispatcher()
        memory_workers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=2),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=memory_worker,  # type: ignore[arg-type]
    )

    status = await runtime.start()
    duplicate = await runtime.start()

    assert status.ready and duplicate.ready
    assert status.memory_outbox_running
    assert status.ai_call_outbox_running
    assert status.purge_worker_running
    assert status.recovered_activations == 2
    assert len(created) == 1
    assert len(memory_workers) == 1
    assert memory_workers[0].started.is_set()
    assert created[0].kwargs["twiml_url_factory"]("token") == (
        "https://phone.example.com/webhooks/twilio/voice/twiml/token"
    )
    assert created[0].kwargs["status_callback_url_factory"]("token") == (
        "https://phone.example.com/webhooks/twilio/voice/status/token"
    )
    assert created[0].kwargs["amd_status_callback_url_factory"]("token") == (
        "https://phone.example.com/webhooks/twilio/voice/amd/token"
    )
    await runtime.stop()
    assert voice.closed == 1
    assert runtime.status().reason == "stopped"
    assert not runtime.status().memory_outbox_running


@pytest.mark.asyncio
async def test_enabled_conversational_workers_share_one_stop_event() -> None:
    voice = _VoiceClient()
    dispatchers: list[_Dispatcher] = []
    memory_workers: list[_Dispatcher] = []
    ai_workers: list[_Dispatcher] = []
    purge = _PurgeWorker()

    def create(target: list[_Dispatcher]):
        def factory(*args: Any, **kwargs: Any) -> _Dispatcher:
            instance = _Dispatcher(*args, **kwargs)
            target.append(instance)
            return instance

        return factory

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=create(dispatchers),
        memory_outbox_worker_factory=create(memory_workers),  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=create(ai_workers),  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )

    assert (await runtime.start()).ready
    assert dispatchers[0].stop_event is memory_workers[0].stop_event
    assert dispatchers[0].stop_event is ai_workers[0].stop_event
    assert dispatchers[0].stop_event is not purge.stop_event
    await runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_start_keeps_exactly_one_owned_dispatcher() -> None:
    voice = _VoiceClient()
    recovery_entered = asyncio.Event()
    release_recovery = asyncio.Event()
    created: list[_Dispatcher] = []

    async def recovery() -> int:
        recovery_entered.set()
        await release_recovery.wait()
        return 0

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        created.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=recovery,
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    first = asyncio.create_task(runtime.start())
    await recovery_entered.wait()
    second = asyncio.create_task(runtime.start())
    release_recovery.set()
    first_status, second_status = await asyncio.gather(first, second)

    assert first_status.ready and second_status.ready
    assert len(created) == 1
    await runtime.stop()


@pytest.mark.asyncio
async def test_readiness_revalidates_dynamic_admin_disable() -> None:
    voice = _VoiceClient()
    enabled = True

    async def config() -> TelephonyConfig:
        return TelephonyConfig(enabled=enabled)

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=config,
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=_Dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready

    enabled = False
    readiness = await runtime.readiness_status()

    assert not readiness.enabled
    assert not readiness.ready
    assert readiness.reason == "disabled"
    await runtime.stop()


@pytest.mark.asyncio
async def test_live_purge_task_without_lease_fails_every_runtime_gate() -> None:
    voice = _VoiceClient()
    purge = _PurgeWorker()
    dispatchers: list[_Dispatcher] = []

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    assert runtime._purge_worker_task is not None
    assert not runtime._purge_worker_task.done()

    purge._runtime_token = None

    readiness = await runtime.readiness_status()
    assert not readiness.ready
    assert readiness.reason == "purge_worker_unavailable"
    assert not await dispatchers[0].kwargs["dispatch_fence"](
        dispatchers[0].dispatcher_id
    )
    with pytest.raises(RuntimeError, match="data purge worker is not running"):
        await dispatchers[0].kwargs["config_loader"]()
    await runtime.stop()


@pytest.mark.asyncio
async def test_dispatcher_config_loader_fences_dynamic_readiness_loss() -> None:
    voice = _VoiceClient()
    operational_error: str | None = None
    created: list[_Dispatcher] = []

    async def probe() -> str | None:
        return operational_error

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        created.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=probe,
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    operational_error = "default_phone_number_missing"

    with pytest.raises(RuntimeError, match="default_phone_number_missing"):
        await created[0].kwargs["config_loader"]()

    assert not await runtime.is_ready()
    await runtime.stop()


@pytest.mark.asyncio
async def test_enabled_runtime_fails_closed_and_cleans_up() -> None:
    voice = _VoiceClient()
    recoveries = 0

    async def recover() -> int:
        nonlocal recoveries
        recoveries += 1
        return 4

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result="default_voice_missing"),
        stale_recovery=recover,
        dispatcher_factory=_Dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.enabled
    assert not status.ready
    assert status.reason == "default_voice_missing"
    assert status.recovered_activations == 4
    assert recoveries == 1
    assert voice.closed == 0
    await runtime.stop()
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_ffmpeg_unavailable_fences_runtime_dispatcher_and_admission() -> None:
    voice = _VoiceClient()
    dispatchers: list[_Dispatcher] = []

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        ffmpeg_probe=lambda: asyncio.sleep(0, result=False),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert status.reason == "ffmpeg_unavailable"
    assert not status.ready
    assert not status.dispatcher_running
    assert not runtime._dispatch_admission_open
    assert dispatchers == []
    assert not await runtime.is_ready()
    await runtime.stop()


@pytest.mark.asyncio
async def test_immediate_dispatcher_failure_is_not_advertised_ready() -> None:
    voice = _VoiceClient()
    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=_FailingDispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )

    status = await runtime.start()

    assert not status.ready
    assert status.reason == "startup_failed:RuntimeError"
    assert voice.closed == 0
    await runtime.stop()
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_outbox_crash_fences_dispatch_gate_and_cancels_dispatcher() -> None:
    voice = _VoiceClient()
    dispatchers: list[_ObservedDispatcher] = []
    outbox = _CrashingOutboxWorker()

    def dispatcher(*args: Any, **kwargs: Any) -> _ObservedDispatcher:
        instance = _ObservedDispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=lambda: outbox,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    assert outbox.started.is_set()

    outbox.crash.set()
    for _ in range(50):
        if runtime.status().reason.startswith("memory_outbox_failed"):
            break
        await asyncio.sleep(0)

    assert runtime.status().reason == "memory_outbox_failed:RuntimeError"
    assert not runtime.status().ready
    assert not await dispatchers[0].kwargs["dispatch_fence"](
        dispatchers[0].dispatcher_id
    )
    with pytest.raises(RuntimeError, match="memory outbox is not running"):
        await dispatchers[0].kwargs["config_loader"]()
    await asyncio.wait_for(dispatchers[0].finished.wait(), timeout=0.5)
    assert runtime._dispatcher_task is not None
    assert runtime._dispatcher_task.cancelled()
    await runtime.stop()


@pytest.mark.asyncio
async def test_ai_call_outbox_crash_fences_readiness_and_dispatcher() -> None:
    voice = _VoiceClient()
    dispatchers: list[_ObservedDispatcher] = []
    memory = _ObservedDispatcher()
    ai_outbox = _CrashingOutboxWorker()

    def dispatcher(*args: Any, **kwargs: Any) -> _ObservedDispatcher:
        instance = _ObservedDispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=lambda: memory,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=lambda: ai_outbox,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    runtime._worker_health_changed.clear()

    ai_outbox.crash.set()
    await asyncio.wait_for(runtime._worker_health_changed.wait(), timeout=0.5)

    assert runtime.status().reason == "ai_call_outbox_failed:RuntimeError"
    assert not runtime.status().ready
    assert not runtime.status().ai_call_outbox_running
    assert not await runtime.is_ready()
    await asyncio.wait_for(dispatchers[0].finished.wait(), timeout=0.5)
    await asyncio.wait_for(memory.finished.wait(), timeout=0.5)
    await asyncio.sleep(0)
    assert runtime._stop_event is not None and runtime._stop_event.is_set()
    assert not runtime.status().dispatcher_running
    assert not runtime.status().memory_outbox_running
    await runtime.stop()


@pytest.mark.asyncio
async def test_unexpected_ai_call_outbox_cancellation_stops_siblings() -> None:
    voice = _VoiceClient()
    dispatcher = _ObservedDispatcher()
    memory = _ObservedDispatcher()
    ai_outbox = _Dispatcher()
    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=lambda *_args, **_kwargs: dispatcher,
        memory_outbox_worker_factory=lambda: memory,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=lambda: ai_outbox,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    runtime._worker_health_changed.clear()
    assert runtime._ai_call_outbox_task is not None

    runtime._ai_call_outbox_task.cancel()
    await asyncio.wait_for(runtime._worker_health_changed.wait(), timeout=0.5)
    await asyncio.wait_for(dispatcher.finished.wait(), timeout=0.5)
    await asyncio.wait_for(memory.finished.wait(), timeout=0.5)
    await asyncio.sleep(0)

    assert runtime.status().reason == "ai_call_outbox_cancelled"
    assert not runtime.status().ready
    assert not runtime.status().ai_call_outbox_running
    assert runtime._stop_event is not None and runtime._stop_event.is_set()
    assert not runtime.status().dispatcher_running
    assert not runtime.status().memory_outbox_running
    await runtime.stop()


@pytest.mark.asyncio
async def test_shutdown_joins_sibling_when_outbox_worker_raises() -> None:
    voice = _VoiceClient()
    dispatcher = _ObservedDispatcher()
    outbox = _ShutdownFailingOutboxWorker()
    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=lambda *_args, **_kwargs: dispatcher,
        memory_outbox_worker_factory=lambda: outbox,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready

    await asyncio.wait_for(runtime.stop(), timeout=0.5)

    assert dispatcher.finished.is_set()
    assert outbox.finished.is_set()
    assert runtime._dispatcher_task is None
    assert runtime._memory_outbox_task is None
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_shutdown_fences_delayed_worker_after_grace_without_orphan(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "integrations.telephony.runtime._RUNTIME_SHUTDOWN_GRACE_SECONDS", 0.02
    )
    voice = _VoiceClient()
    dispatcher = _DelayedCancellationDispatcher()
    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=lambda *_args, **_kwargs: dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready

    await asyncio.wait_for(runtime.stop(), timeout=0.5)
    try:
        assert dispatcher.cancel_started.is_set()
        assert runtime._dispatcher_task is dispatcher.owned_task
        assert runtime._memory_outbox_task is None
        assert runtime.status().reason == "stop_incomplete"
        assert runtime.status().dispatcher_running
        assert voice.closed == 1
        assert dispatcher.owned_task is not None
        assert not dispatcher.owned_task.done()
    finally:
        dispatcher.release_cleanup.set()
    for _ in range(50):
        if dispatcher.owned_task.done():
            break
        await asyncio.sleep(0)
    assert dispatcher.owned_task.done()
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_dynamic_disable_fences_dispatch_before_worker_stop() -> None:
    voice = _VoiceClient()
    enabled = True
    created: list[_Dispatcher] = []

    async def config() -> TelephonyConfig:
        return TelephonyConfig(enabled=enabled)

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        created.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=config,
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    enabled = False

    assert not await created[0].kwargs["dispatch_fence"](
        created[0].dispatcher_id
    )
    with pytest.raises(RuntimeError, match="disabled"):
        await created[0].kwargs["config_loader"]()

    status = await runtime.reconcile()
    assert not status.enabled
    assert not status.ready
    assert status.reason == "disabled"
    assert runtime._dispatcher_task is None
    assert runtime._memory_outbox_task is None
    assert voice.closed == 0
    await runtime.stop()
    assert voice.closed == 1


@pytest.mark.asyncio
async def test_purge_worker_survives_disable_and_reenable_reconcile() -> None:
    voice = _VoiceClient()
    enabled = False
    purge_workers: list[_PurgeWorker] = []
    dispatchers: list[_Dispatcher] = []

    async def config() -> TelephonyConfig:
        return TelephonyConfig(enabled=enabled)

    def purge_factory() -> _PurgeWorker:
        worker = _PurgeWorker()
        purge_workers.append(worker)
        return worker

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        worker = _Dispatcher(*args, **kwargs)
        dispatchers.append(worker)
        return worker

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=config,
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=_Dispatcher,  # type: ignore[arg-type]
        purge_worker_factory=purge_factory,  # type: ignore[arg-type]
    )

    disabled = await runtime.start()
    purge_task = runtime._purge_worker_task
    assert disabled.reason == "disabled"
    assert disabled.purge_worker_running

    enabled = True
    assert (await runtime.reconcile()).ready
    enabled = False
    disabled_again = await runtime.reconcile()
    assert disabled_again.reason == "disabled"
    assert disabled_again.purge_worker_running
    assert runtime._purge_worker_task is purge_task
    assert purge_task is not None and not purge_task.done()

    enabled = True
    assert (await runtime.reconcile()).ready
    assert runtime._purge_worker_task is purge_task
    assert len(purge_workers) == 1
    assert len(dispatchers) == 2
    await runtime.stop()


@pytest.mark.asyncio
async def test_purge_worker_death_fences_and_stops_conversational_workers() -> None:
    voice = _VoiceClient()
    purge = _CrashingPurgeWorker()
    dispatcher = _ObservedDispatcher()
    memory = _ObservedDispatcher()
    ai_outbox = _ObservedDispatcher()
    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=lambda: asyncio.sleep(0, result=TelephonyConfig(enabled=True)),
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=lambda *_args, **_kwargs: dispatcher,
        memory_outbox_worker_factory=lambda: memory,  # type: ignore[arg-type]
        ai_call_outbox_worker_factory=lambda: ai_outbox,  # type: ignore[arg-type]
        purge_worker_factory=lambda: purge,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    runtime._worker_health_changed.clear()

    purge.crash.set()
    await asyncio.wait_for(runtime._worker_health_changed.wait(), timeout=0.5)

    assert runtime.status().reason == "purge_worker_failed:RuntimeError"
    assert not runtime.status().ready
    assert not runtime.status().purge_worker_running
    await asyncio.wait_for(dispatcher.finished.wait(), timeout=0.5)
    await asyncio.wait_for(memory.finished.wait(), timeout=0.5)
    await asyncio.wait_for(ai_outbox.finished.wait(), timeout=0.5)
    await runtime.stop()


@pytest.mark.asyncio
async def test_concurrent_reconfigure_replaces_workers_once() -> None:
    voice = _VoiceClient()
    config = TelephonyConfig(
        enabled=True,
        scheduler_jitter_seconds=10,
        max_concurrent_dispatches=12,
    )
    dispatchers: list[_Dispatcher] = []
    memory_workers: list[_Dispatcher] = []

    async def load_config() -> TelephonyConfig:
        return config

    def dispatcher(*args: Any, **kwargs: Any) -> _Dispatcher:
        instance = _Dispatcher(*args, **kwargs)
        dispatchers.append(instance)
        return instance

    def memory_worker() -> _Dispatcher:
        instance = _Dispatcher()
        memory_workers.append(instance)
        return instance

    runtime = TelephonyRuntime(
        _provider(voice),
        config_loader=load_config,
        readiness_probe=lambda: asyncio.sleep(0, result=None),
        stale_recovery=lambda: asyncio.sleep(0, result=0),
        dispatcher_factory=dispatcher,
        memory_outbox_worker_factory=memory_worker,  # type: ignore[arg-type]
    )
    assert (await runtime.start()).ready
    config = TelephonyConfig(
        enabled=True,
        scheduler_jitter_seconds=11,
        max_concurrent_dispatches=23,
    )

    first, second = await asyncio.gather(runtime.reconcile(), runtime.reconcile())

    assert first.ready and second.ready
    assert len(dispatchers) == 2
    assert len(memory_workers) == 2
    assert dispatchers[-1].kwargs["jitter_seconds"] == 11
    assert dispatchers[-1].kwargs["max_concurrent_dispatches"] == 23
    assert voice.closed == 0
    await runtime.stop()
    await runtime.stop()
    assert voice.closed == 1


def test_integration_router_registers_owner_and_provider_phone_routes() -> None:
    paths = {route.path for route in integration_routes.router.routes}

    assert "/api/telephony/contacts" in paths
    assert "/api/conversations/{conversation_id}/phone-calls" in paths
    assert "/webhooks/twilio/voice/inbound" in paths
    assert "/ws/twilio/media-stream" in paths
