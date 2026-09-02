from __future__ import annotations

import asyncio
from typing import Any

import pytest

from integrations.telephony.ffmpeg import is_ffmpeg_available


class _HangingProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = 0
        self.killed = 0
        self._finished = asyncio.Event()

    async def wait(self) -> int:
        await self._finished.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9
        self._finished.set()


@pytest.mark.asyncio
async def test_ffmpeg_probe_accepts_successful_executable() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class Process:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def create(*args: Any, **kwargs: Any) -> Process:
        calls.append((args, kwargs))
        return Process()

    assert await is_ffmpeg_available(process_factory=create)
    assert calls[0][0] == ("ffmpeg", "-version")
    assert calls[0][1]["stdin"] is asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_ffmpeg_probe_times_out_then_kills_and_reaps_process() -> None:
    process = _HangingProcess()

    async def create(*_args: Any, **_kwargs: Any) -> _HangingProcess:
        return process

    assert not await is_ffmpeg_available(
        timeout_seconds=0.001,
        terminate_grace_seconds=0.001,
        kill_grace_seconds=0.1,
        process_factory=create,
    )
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9


@pytest.mark.asyncio
async def test_ffmpeg_probe_rejects_missing_executable() -> None:
    async def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError

    assert not await is_ffmpeg_available(process_factory=missing)


@pytest.mark.asyncio
async def test_ffmpeg_probe_bounds_and_joins_hanging_factory() -> None:
    factory_finished = asyncio.Event()

    async def hanging(*_args: Any, **_kwargs: Any) -> Any:
        try:
            await asyncio.Event().wait()
        finally:
            factory_finished.set()

    assert not await is_ffmpeg_available(
        timeout_seconds=0.001,
        process_factory=hanging,
    )
    assert factory_finished.is_set()
    assert not _probe_tasks()


@pytest.mark.asyncio
async def test_ffmpeg_probe_wait_failure_still_kills_and_reaps() -> None:
    class Process(_HangingProcess):
        def __init__(self) -> None:
            super().__init__()
            self.wait_calls = 0

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise RuntimeError("wait failed")
            return await super().wait()

    process = Process()

    async def create(*_args: Any, **_kwargs: Any) -> Process:
        return process

    assert not await is_ffmpeg_available(
        terminate_grace_seconds=0.001,
        kill_grace_seconds=0.1,
        process_factory=create,
    )
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert process.wait_calls == 3
    assert not _probe_tasks()


@pytest.mark.asyncio
async def test_ffmpeg_probe_cancellation_during_spawn_joins_factory_task() -> None:
    factory_started = asyncio.Event()
    factory_finished = asyncio.Event()
    process = _HangingProcess()

    async def spawning(*_args: Any, **_kwargs: Any) -> _HangingProcess:
        factory_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Model the race where process creation completed as cancellation
            # arrived. The owned factory task must hand the live process back.
            return process
        finally:
            factory_finished.set()

    probe = asyncio.create_task(
        is_ffmpeg_available(
            terminate_grace_seconds=0.001,
            kill_grace_seconds=0.1,
            process_factory=spawning,
        ),
    )
    await factory_started.wait()
    probe.cancel()

    with pytest.raises(asyncio.CancelledError):
        await probe
    assert factory_finished.is_set()
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert not _probe_tasks()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_spawn_recovers_live_process() -> None:
    factory_started = asyncio.Event()
    first_cancel_received = asyncio.Event()
    release_factory = asyncio.Event()
    process = _HangingProcess()

    async def spawning(*_args: Any, **_kwargs: Any) -> _HangingProcess:
        factory_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            first_cancel_received.set()
            await release_factory.wait()
            return process

    probe = asyncio.create_task(
        is_ffmpeg_available(
            terminate_grace_seconds=0.001,
            kill_grace_seconds=0.1,
            process_factory=spawning,
        )
    )
    await factory_started.wait()
    probe.cancel()
    await first_cancel_received.wait()
    probe.cancel()
    release_factory.set()

    with pytest.raises(asyncio.CancelledError):
        await probe
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert not _probe_tasks()


@pytest.mark.asyncio
async def test_repeated_cancellation_during_reap_cannot_abandon_process() -> None:
    class Process(_HangingProcess):
        def __init__(self) -> None:
            super().__init__()
            self.wait_calls = 0
            self.initial_wait_started = asyncio.Event()
            self.terminate_reap_started = asyncio.Event()

        async def wait(self) -> int:
            self.wait_calls += 1
            if self.wait_calls == 1:
                self.initial_wait_started.set()
            elif self.wait_calls == 2:
                self.terminate_reap_started.set()
            return await super().wait()

    process = Process()

    async def create(*_args: Any, **_kwargs: Any) -> Process:
        return process

    probe = asyncio.create_task(
        is_ffmpeg_available(
            timeout_seconds=10.0,
            terminate_grace_seconds=0.05,
            kill_grace_seconds=0.1,
            process_factory=create,
        )
    )
    await process.initial_wait_started.wait()
    probe.cancel()
    await process.terminate_reap_started.wait()
    probe.cancel()

    with pytest.raises(asyncio.CancelledError):
        await probe
    assert process.terminated == 1
    assert process.killed == 1
    assert process.returncode == -9
    assert process.wait_calls == 3
    assert not _probe_tasks()


def _probe_tasks() -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    return [
        task
        for task in asyncio.all_tasks()
        if task is not current and task.get_name().startswith("aurvek-ffmpeg-probe-")
    ]
