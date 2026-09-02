"""Bounded ownership helpers for native telephone lifecycle tasks."""

from __future__ import annotations

import asyncio
from asyncio.tasks import wait as _wait_tasks
from collections.abc import Iterable
from typing import Any


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, Exception):
        pass


async def cancel_and_join_tasks(
    tasks: Iterable[asyncio.Task[Any]],
    *,
    deadline: float,
    cancel_first: bool = True,
) -> tuple[asyncio.Task[Any], ...]:
    """Cancel owned tasks and join them only until one loop-time deadline.

    Internal workers are required to be cancellation-cooperative.  A broken
    external adapter cannot hold shutdown forever: any survivor is returned to
    its owner for fencing/reporting and gets a result consumer so it cannot
    emit an unobserved exception after ownership references are cleared.
    """

    current = asyncio.current_task()
    owned = tuple(
        dict.fromkeys(task for task in tasks if task is not current)
    )
    if cancel_first:
        for task in owned:
            if not task.done():
                task.cancel()
    if not owned:
        return ()

    timeout = max(0.0, deadline - asyncio.get_running_loop().time())
    try:
        done, pending = await _wait_tasks(owned, timeout=timeout)
    except asyncio.CancelledError:
        for task in owned:
            if not task.done():
                task.cancel()
                task.add_done_callback(_consume_task_result)
        raise
    for task in done:
        _consume_task_result(task)
    if pending:
        for task in pending:
            task.cancel()
        # Deliver the final cancellation without creating another unbounded
        # wait. Cooperative workers finish in this checkpoint.
        await asyncio.sleep(0)

    survivors = tuple(task for task in owned if not task.done())
    for task in survivors:
        task.add_done_callback(_consume_task_result)
    return survivors


__all__ = ["cancel_and_join_tasks"]
