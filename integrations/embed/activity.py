"""Observe the existing streaming lifecycle and reconcile explicit stop.

Embed v1 requires one application worker, matching the native stop registry.
The tracked span includes response streaming, persistence and billing cleanup.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


@dataclass
class ActiveGeneration:
    conversation_id: int
    user_id: int
    delegated_session_id: str
    ending: bool = False
    response_finished: bool = False
    producers: set[asyncio.Task] = field(default_factory=set)
    watcher: asyncio.Task | None = None
    principal: Any = None


_active: dict[int, ActiveGeneration] = {}
_ended_sessions: dict[str, float] = {}
_WATCH_ACCESS_INTERVAL = 2
_current_generation: ContextVar[ActiveGeneration | None] = ContextVar(
    "embed_generation", default=None,
)


def _finish_generation_if_idle(active: ActiveGeneration) -> asyncio.Task | None:
    """Release observation only after HTTP and every billable producer finish."""
    if not active.response_finished or active.producers:
        return None
    watcher = active.watcher
    if watcher is not None:
        watcher.cancel()
    if _active.get(active.conversation_id) is active:
        del _active[active.conversation_id]
    return watcher


def retain_embed_producer(producer: asyncio.Task) -> None:
    """Attach native detached billing work to the inherited embed activity.

    The billing wrapper deliberately keeps its producer running when the HTTP
    consumer disconnects. Context propagates into that producer and any nested
    billing tasks, while native requests without embed context remain unchanged.
    """
    active = _current_generation.get()
    if active is None:
        return
    active.producers.add(producer)

    def finished(task):
        active.producers.discard(task)
        _finish_generation_if_idle(active)

    producer.add_done_callback(finished)


async def _revalidate(principal) -> None:
    from .identity import get_embed_store

    await get_embed_store().revalidate_principal(principal)


async def check_embed_generation_active() -> None:
    """Fence work after an awaited billing lock or native preflight step."""
    active = _current_generation.get()
    if active is None:
        return
    await _revalidate(active.principal)
    # Check again after the DB await: logout/stop closes admission synchronously.
    if active.ending or active.delegated_session_id in _ended_sessions:
        raise HTTPException(status_code=401, detail="unauthenticated")


async def _watch_access(principal, active: ActiveGeneration) -> None:
    # Server-side supervision exists only while a paid stream is active.
    # A registry outage or revocation requests native cancellation, never more work.
    while True:
        await asyncio.sleep(_WATCH_ACCESS_INTERVAL)
        try:
            await _revalidate(principal)
            if not active.ending:
                continue
        except Exception:
            pass
        try:
            await _signal_stop(active)
        except Exception:
            pass
        # Keep reconciling through actual completion. A provider or a queued
        # native preflight may reset its stop registry after our first signal.


@asynccontextmanager
async def track_embed_generation(principal):
    await _revalidate(principal)
    now = time.time()
    for session_id, expiry in list(_ended_sessions.items()):
        if expiry <= now:
            del _ended_sessions[session_id]
    if str(principal.delegated_session_id) in _ended_sessions:
        raise HTTPException(status_code=401, detail="unauthenticated")
    conversation_id = int(principal.conversation_id)
    from integrations.applications.activity import check_application_admission
    check_application_admission(conversation_id)
    # No await between the admission check and insertion: one event loop/worker.
    if conversation_id in _active:
        raise HTTPException(status_code=409, detail="activity_in_progress")
    active = ActiveGeneration(
        conversation_id, int(principal.user_id), str(principal.delegated_session_id),
        principal=principal,
    )
    _active[conversation_id] = active
    watcher = asyncio.create_task(_watch_access(principal, active))
    active.watcher = watcher
    token = _current_generation.set(active)
    try:
        yield
    finally:
        _current_generation.reset(token)
        active.response_finished = True
        completed_watcher = _finish_generation_if_idle(active)
        if completed_watcher is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await completed_watcher


async def activity_state(conversation_id: int) -> str:
    active = _active.get(int(conversation_id))
    return "ending" if active and active.ending else "generating" if active else "idle"


async def get_activity(principal) -> dict[str, Any]:
    state = await activity_state(int(principal.conversation_id))
    return {"activity": state, "closed": state == "idle"}


async def _signal_stop(active: ActiveGeneration) -> None:
    if _active.get(active.conversation_id) is not active:
        return
    active.ending = True
    from integrations.applications.activity import mark_application_stop
    mark_application_stop(active.conversation_id, active.user_id)
    from chat.services.stop_signals import stop_signals

    stop_signals[active.conversation_id] = True
    # Reuse the native stop/reconciliation path; its response only means that
    # the stop was requested. Completion is observed separately above.
    from chat.routes.conversations import stop_current_generation
    from auth import get_user_by_id

    user = await get_user_by_id(active.user_id)
    if user is None:
        raise HTTPException(status_code=503, detail="service_unavailable")
    await stop_current_generation(
        active.conversation_id, user,
        is_current=lambda: _active.get(active.conversation_id) is active,
    )


def mark_embed_stop(principal) -> ActiveGeneration | None:
    """Preserve native stop intent before its asynchronous control path starts."""
    active = _active.get(int(principal.conversation_id))
    if active:
        if active.user_id != int(principal.user_id):
            raise HTTPException(status_code=404, detail="not_found")
        active.ending = True
        from integrations.applications.activity import mark_application_stop
        mark_application_stop(active.conversation_id, active.user_id)
    return active


async def request_stop(principal) -> dict[str, Any]:
    active = mark_embed_stop(principal)
    if active:
        await _signal_stop(active)
    return await get_activity(principal)


async def end_session_activity(
    delegated_session_id: str, user_id: int,
) -> dict[str, Any]:
    # Close admission before awaiting anything: a previously successful DB read
    # must not start a stream after logout has acknowledged that session closed.
    _ended_sessions[str(delegated_session_id)] = time.time() + 8 * 3600
    matching = [
        item for item in list(_active.values())
        if item.delegated_session_id == str(delegated_session_id)
        and item.user_id == int(user_id)
    ]
    await asyncio.gather(*(_signal_stop(item) for item in matching))
    pending = any(
        item.delegated_session_id == str(delegated_session_id)
        and item.user_id == int(user_id) for item in _active.values()
    )
    return {"activity": "ending" if pending else "idle", "closed": not pending}


async def close_delegated_session(store, app_id, credential):
    """Revoke the delegation and close activity through either backend contract."""
    session = await store.session_for_logout(app_id, credential)
    await store.end_app_session(app_id, credential)
    state = {"activity": "idle", "closed": True}
    if session:
        state = await end_session_activity(session["session_id"], session["user_id"])
    return {"app_id": app_id, "status": "revoked", **state}
