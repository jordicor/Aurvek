"""Bounded readiness probe for the ffmpeg executable used by telephony."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


ProcessFactory = Callable[..., Awaitable[Any]]


class FfmpegProcessError(RuntimeError):
    """An owned ffmpeg process could not be spawned or observed safely."""


class FfmpegProcessTimeout(FfmpegProcessError):
    """An owned ffmpeg process did not finish before its deadline."""


class FfmpegProcessOwnershipError(FfmpegProcessError):
    """An owned ffmpeg process could not be confirmed stopped and reaped."""


@dataclass(frozen=True, slots=True)
class _JoinOutcome:
    result: Any | None
    outer_cancelled: bool
    child_cancelled: bool
    error: Exception | None


async def run_owned_ffmpeg(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
    process_factory: ProcessFactory = asyncio.create_subprocess_exec,
    process_kwargs: Mapping[str, Any] | None = None,
    task_name_prefix: str = "aurvek-ffmpeg",
) -> int:
    """Spawn, bound, stop and reap one ffmpeg process without abandonment."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    process: Any | None = None
    spawn_task = asyncio.create_task(
        process_factory(*command, **dict(process_kwargs or {})),
        name=f"{task_name_prefix}-spawn",
    )
    try:
        try:
            done, _pending = await asyncio.wait(
                (spawn_task,),
                timeout=max(0.0, deadline - loop.time()),
            )
        except asyncio.CancelledError:
            process = (await _cancel_and_join(spawn_task)).result
            raise
        if not done:
            outcome = await _cancel_and_join(spawn_task)
            process = outcome.result
            if outcome.outer_cancelled:
                raise asyncio.CancelledError
            if outcome.error is not None:
                raise FfmpegProcessError(
                    "ffmpeg process spawn failed"
                ) from outcome.error
            raise FfmpegProcessTimeout("ffmpeg process spawn timed out")
        try:
            process = spawn_task.result()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise FfmpegProcessError("ffmpeg process spawn failed") from exc

        remaining = max(0.0, deadline - loop.time())
        if remaining <= 0:
            raise FfmpegProcessTimeout("ffmpeg process timed out")
        wait_task = asyncio.create_task(
            process.wait(),
            name=f"{task_name_prefix}-wait",
        )
        try:
            try:
                done, _pending = await asyncio.wait(
                    (wait_task,),
                    timeout=remaining,
                )
            except asyncio.CancelledError:
                await _cancel_and_join(wait_task)
                raise
            if not done:
                outcome = await _cancel_and_join(wait_task)
                if outcome.outer_cancelled:
                    raise asyncio.CancelledError
                raise FfmpegProcessTimeout("ffmpeg process timed out")
            try:
                return int(wait_task.result())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise FfmpegProcessError("ffmpeg process wait failed") from exc
        finally:
            if not wait_task.done():
                outcome = await _cancel_and_join(wait_task)
                if outcome.outer_cancelled:
                    raise asyncio.CancelledError
    finally:
        if process is not None and getattr(process, "returncode", None) is None:
            await _stop_process_non_abandonable(
                process,
                terminate_grace_seconds=terminate_grace_seconds,
                kill_grace_seconds=kill_grace_seconds,
                task_name_prefix=task_name_prefix,
            )


async def is_ffmpeg_available(
    *,
    ffmpeg_binary: str = "ffmpeg",
    timeout_seconds: float = 2.0,
    terminate_grace_seconds: float = 0.5,
    kill_grace_seconds: float = 0.5,
    process_factory: ProcessFactory = asyncio.create_subprocess_exec,
) -> bool:
    """Return whether ffmpeg starts and exits successfully within a hard bound."""

    if (
        not ffmpeg_binary.strip()
        or timeout_seconds <= 0
        or terminate_grace_seconds <= 0
        or kill_grace_seconds <= 0
    ):
        return False

    try:
        returncode = await run_owned_ffmpeg(
            (ffmpeg_binary, "-version"),
            timeout_seconds=timeout_seconds,
            terminate_grace_seconds=terminate_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
            process_factory=process_factory,
            process_kwargs={
                "stdin": asyncio.subprocess.DEVNULL,
                "stdout": asyncio.subprocess.DEVNULL,
                "stderr": asyncio.subprocess.DEVNULL,
            },
            task_name_prefix="aurvek-ffmpeg-probe",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    return returncode == 0


async def _stop_process_non_abandonable(
    process: Any,
    *,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
    task_name_prefix: str,
) -> None:
    cleanup_task = asyncio.create_task(
        _stop_process(
            process,
            terminate_grace_seconds=terminate_grace_seconds,
            kill_grace_seconds=kill_grace_seconds,
        ),
        name=f"{task_name_prefix}-cleanup",
    )
    outcome = await _join_non_abandonable(cleanup_task)
    if outcome.error is not None:
        raise outcome.error
    if outcome.outer_cancelled or outcome.child_cancelled:
        raise asyncio.CancelledError


async def _stop_process(
    process: Any,
    *,
    terminate_grace_seconds: float,
    kill_grace_seconds: float,
) -> None:
    cancellation_pending = False
    try:
        process.terminate()
    except Exception:
        pass
    try:
        exited = await _wait_for_exit(
            process,
            timeout_seconds=terminate_grace_seconds,
        )
    except asyncio.CancelledError:
        cancellation_pending = True
        exited = False
    if exited and getattr(process, "returncode", None) is not None:
        if cancellation_pending:
            raise asyncio.CancelledError
        return

    if getattr(process, "returncode", None) is None:
        try:
            process.kill()
        except Exception:
            pass
    try:
        exited = await _wait_for_exit(
            process,
            timeout_seconds=kill_grace_seconds,
        )
    except asyncio.CancelledError:
        cancellation_pending = True
        exited = False
    if cancellation_pending:
        raise asyncio.CancelledError
    if not exited or getattr(process, "returncode", None) is None:
        raise FfmpegProcessOwnershipError(
            "ffmpeg process did not exit after kill"
        )


async def _wait_for_exit(process: Any, *, timeout_seconds: float) -> bool:
    wait_task = asyncio.create_task(
        process.wait(),
        name="aurvek-ffmpeg-probe-reap",
    )
    try:
        try:
            done, _pending = await asyncio.wait(
                (wait_task,),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            await _cancel_and_join(wait_task)
            raise
        if not done:
            outcome = await _cancel_and_join(wait_task)
            if outcome.outer_cancelled:
                raise asyncio.CancelledError
            return False
        try:
            wait_task.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True
    finally:
        if not wait_task.done():
            outcome = await _cancel_and_join(wait_task)
            if outcome.outer_cancelled:
                raise asyncio.CancelledError


async def _cancel_and_join(task: asyncio.Task[Any]) -> _JoinOutcome:
    if not task.done():
        task.cancel()
    return await _join_non_abandonable(task)


async def _join_non_abandonable(
    task: asyncio.Task[Any],
) -> _JoinOutcome:
    current = asyncio.current_task()
    seen_cancellations = current.cancelling() if current is not None else 0
    cancellation_pending = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current_cancellations = (
                current.cancelling() if current is not None else seen_cancellations
            )
            if current_cancellations > seen_cancellations:
                cancellation_pending = True
                seen_cancellations = current_cancellations
        except Exception:
            break
    try:
        result = task.result()
    except asyncio.CancelledError:
        child_cancelled = True
        result = None
        error = None
    except Exception as exc:
        child_cancelled = False
        result = None
        error = exc
    else:
        child_cancelled = False
        error = None
    return _JoinOutcome(
        result=result,
        outer_cancelled=cancellation_pending,
        child_cancelled=child_cancelled,
        error=error,
    )


__all__ = [
    "FfmpegProcessError",
    "FfmpegProcessOwnershipError",
    "FfmpegProcessTimeout",
    "is_ffmpeg_available",
    "run_owned_ffmpeg",
]
