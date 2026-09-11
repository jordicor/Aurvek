"""One-worker application turn boundaries, backed by native billing lifetime.

An operation remains active through queued preflight, deferred streaming and
settlement. Stop flags deliberately do not indicate activity: native flags can
outlive the generation that created them. This registry owns no provider tasks.
"""
from __future__ import annotations

import asyncio
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from integrations.applications.models import ApplicationContext
from integrations.embed.models import EmbedError


@dataclass
class ActiveOperation:
    scope: ApplicationContext
    operation_id: str
    references: int = 1
    ending: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    sealed: bool = False
    pending_handoff: object | None = None
    completion: object | None = None


_active: dict[int, ActiveOperation] = {}
_boundaries: dict[int, object] = {}


def get_active_operation(conversation_id: int) -> ActiveOperation | None:
    return _active.get(int(conversation_id))


def check_application_admission(conversation_id: int, operation=None) -> None:
    conversation_id = int(conversation_id)
    active = _active.get(conversation_id)
    from integrations.embed.activity import _active as embed_active, _current_generation
    embed = embed_active.get(conversation_id)
    if embed is not None and _current_generation.get() is not embed:
        raise EmbedError("activity_in_progress", 409)
    if conversation_id in _boundaries or (
        active is not None and (
            operation is None or active.operation_id != operation.operation_id
            or active.scope != operation.scope or active.ending or active.sealed
        )
    ):
        raise EmbedError("activity_in_progress", 409)


def begin_application_admission(scope: ApplicationContext) -> ActiveOperation:
    """Reserve the conversation before awaited funding admission."""
    check_application_admission(scope.conversation_id)
    active = ActiveOperation(scope, secrets.token_urlsafe(24))
    _active[int(scope.conversation_id)] = active
    return active


def bind_application_admission(active: ActiveOperation, operation) -> None:
    """Upgrade provisional admission to the immutable funding operation."""
    if _active.get(int(active.scope.conversation_id)) is not active:
        raise EmbedError("activity_in_progress", 409)
    if active.ending:
        raise EmbedError("activity_in_progress", 409)
    active.scope = operation.scope
    active.operation_id = operation.operation_id


def begin_application_operation(operation) -> ActiveOperation | None:
    if operation is None:
        return None
    check_application_admission(operation.scope.conversation_id, operation)
    active = _active.get(int(operation.scope.conversation_id))
    if active is None:
        active = ActiveOperation(operation.scope, operation.operation_id)
        _active[int(operation.scope.conversation_id)] = active
    else:
        active.references += 1
    return active


def end_application_operation(active: ActiveOperation | None) -> None:
    if active is None:
        return
    active.references -= 1
    if active.references == 0:
        if _active.get(int(active.scope.conversation_id)) is active:
            del _active[int(active.scope.conversation_id)]
        active.done.set()


def mark_application_stop(conversation_id: int, user_id: int) -> ActiveOperation | None:
    """Close admission synchronously; native stop still owns cancellation."""
    active = _active.get(int(conversation_id))
    if active is not None:
        if active.scope.user_id != int(user_id):
            raise EmbedError("not_found", 404)
        active.ending = True
    return active


def check_application_generation_active(operation) -> None:
    if operation is not None:
        check_application_admission(operation.scope.conversation_id, operation)


async def activity_state(conversation_id: int) -> str:
    active = _active.get(int(conversation_id))
    return "ending" if active and active.ending else "generating" if active else "idle"


@dataclass(frozen=True)
class CompletedApplicationTurn:
    """Internal capability issued after the source transcript and money settle."""
    active: ActiveOperation
    embed_generation: object | None


def seal_application_turn(operation) -> CompletedApplicationTurn:
    from integrations.embed.activity import _current_generation
    active = get_active_operation(operation.scope.conversation_id)
    if (active is None or active.operation_id != operation.operation_id
            or active.scope != operation.scope or active.sealed or active.ending):
        raise EmbedError("activity_in_progress", 409)
    active.sealed = True
    token = CompletedApplicationTurn(active, _current_generation.get())
    active.completion = token
    return token


class HandoffBoundary:
    def __init__(self, completed_turn=None):
        self.conversation_ids: set[int] = set()
        self.completed_turn = completed_turn

    def include(self, conversation_id: int) -> None:
        """Add a destination discovered inside the caller's transaction."""
        conversation_id = int(conversation_id)
        if conversation_id in self.conversation_ids:
            return
        # Embed admission precedes canonical app admission and also covers
        # revocation watchers and pending HTTP cleanup for that frame.
        from integrations.embed.activity import _active as embed_active

        active = _active.get(conversation_id)
        embed = embed_active.get(conversation_id)
        proof = self.completed_turn
        own_completed_source = (
            isinstance(proof, CompletedApplicationTurn) and active is proof.active
            and active.completion is proof and active.sealed
            and (embed is None or embed is proof.embed_generation)
        )
        if (conversation_id in _boundaries or
                ((active is not None or embed is not None) and not own_completed_source)):
            raise EmbedError("activity_in_progress", 409)
        _boundaries[conversation_id] = self
        self.conversation_ids.add(conversation_id)

    def close(self) -> None:
        for conversation_id in self.conversation_ids:
            if _boundaries.get(conversation_id) is self:
                del _boundaries[conversation_id]
        self.conversation_ids.clear()


@asynccontextmanager
async def handoff_boundary(source_id: int, destination_id: int | None = None, *, completed_turn=None):
    """Fence both conversations until the caller's handoff transaction commits.

    A busy turn must finish or be stopped/reconciled first. Rejection is atomic
    in the supported single application worker; no database/network await occurs
    between checking activity and reserving the conversation.
    """
    boundary = HandoffBoundary(completed_turn)
    try:
        boundary.include(source_id)
        if destination_id is not None:
            boundary.include(destination_id)
        yield boundary
    finally:
        boundary.close()
