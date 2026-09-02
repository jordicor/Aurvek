"""State definitions shared by the telephony repository and later adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PhoneJobStatus(StrEnum):
    SCHEDULED = "scheduled"
    DISPATCHING = "dispatching"
    COMPLETED = "completed"
    CANCELED = "canceled"
    MISSED = "missed"
    CONFLICT = "conflict"
    NEEDS_ATTENTION = "needs_attention"


class PhoneCallStatus(StrEnum):
    CREATED = "created"
    DISPATCHING = "dispatching"
    DISPATCH_UNKNOWN = "dispatch_unknown"
    QUEUED = "queued"
    INITIATED = "initiated"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BUSY = "busy"
    NO_ANSWER = "no_answer"
    MACHINE = "machine"
    FAILED = "failed"
    CANCELED = "canceled"
    UNRESOLVED = "unresolved"


JOB_TERMINAL_STATUSES = frozenset(
    {
        PhoneJobStatus.COMPLETED,
        PhoneJobStatus.CANCELED,
        PhoneJobStatus.MISSED,
        PhoneJobStatus.CONFLICT,
    }
)

CALL_TERMINAL_STATUSES = frozenset(
    {
        PhoneCallStatus.COMPLETED,
        PhoneCallStatus.BUSY,
        PhoneCallStatus.NO_ANSWER,
        PhoneCallStatus.MACHINE,
        PhoneCallStatus.FAILED,
        PhoneCallStatus.CANCELED,
        PhoneCallStatus.UNRESOLVED,
    }
)

CALL_INCOMPATIBLE_STATUSES = frozenset(
    {
        PhoneCallStatus.CREATED,
        PhoneCallStatus.DISPATCHING,
        PhoneCallStatus.DISPATCH_UNKNOWN,
        PhoneCallStatus.QUEUED,
        PhoneCallStatus.INITIATED,
        PhoneCallStatus.RINGING,
        PhoneCallStatus.IN_PROGRESS,
        PhoneCallStatus.UNRESOLVED,
    }
)

_CALL_PROGRESS = {
    PhoneCallStatus.CREATED: 0,
    PhoneCallStatus.DISPATCHING: 1,
    PhoneCallStatus.QUEUED: 2,
    PhoneCallStatus.INITIATED: 3,
    PhoneCallStatus.RINGING: 4,
    PhoneCallStatus.IN_PROGRESS: 5,
    PhoneCallStatus.COMPLETED: 6,
}

_JOB_TRANSITIONS = {
    PhoneJobStatus.SCHEDULED: {
        PhoneJobStatus.DISPATCHING,
        PhoneJobStatus.CANCELED,
        PhoneJobStatus.MISSED,
        PhoneJobStatus.CONFLICT,
    },
    PhoneJobStatus.DISPATCHING: {
        PhoneJobStatus.COMPLETED,
        PhoneJobStatus.CANCELED,
        PhoneJobStatus.MISSED,
        PhoneJobStatus.CONFLICT,
        PhoneJobStatus.NEEDS_ATTENTION,
    },
    PhoneJobStatus.NEEDS_ATTENTION: {
        PhoneJobStatus.COMPLETED,
        PhoneJobStatus.CANCELED,
    },
}


def normalize_job_status(value: str | PhoneJobStatus) -> PhoneJobStatus:
    return value if isinstance(value, PhoneJobStatus) else PhoneJobStatus(value)


def normalize_call_status(value: str | PhoneCallStatus) -> PhoneCallStatus:
    return value if isinstance(value, PhoneCallStatus) else PhoneCallStatus(value)


def can_transition_job(
    current: str | PhoneJobStatus,
    target: str | PhoneJobStatus,
) -> bool:
    current_status = normalize_job_status(current)
    target_status = normalize_job_status(target)
    return current_status == target_status or target_status in _JOB_TRANSITIONS.get(
        current_status, set()
    )


def call_transition_result(
    current: str | PhoneCallStatus,
    target: str | PhoneCallStatus,
) -> str:
    """Return ``apply``, ``noop`` or ``invalid`` for a callback transition."""
    current_status = normalize_call_status(current)
    target_status = normalize_call_status(target)
    if current_status == target_status:
        return "noop"
    if current_status in CALL_TERMINAL_STATUSES:
        return "invalid"
    if current_status == PhoneCallStatus.DISPATCH_UNKNOWN:
        if target_status in {
            PhoneCallStatus.QUEUED,
            PhoneCallStatus.INITIATED,
            PhoneCallStatus.RINGING,
            PhoneCallStatus.IN_PROGRESS,
            *CALL_TERMINAL_STATUSES,
        }:
            return "apply"
        return "invalid"
    if target_status == PhoneCallStatus.DISPATCH_UNKNOWN:
        return "apply" if current_status == PhoneCallStatus.DISPATCHING else "invalid"
    if target_status == PhoneCallStatus.UNRESOLVED:
        return "invalid"
    if target_status in CALL_TERMINAL_STATUSES:
        return "apply"
    current_rank = _CALL_PROGRESS.get(current_status)
    target_rank = _CALL_PROGRESS.get(target_status)
    if current_rank is None or target_rank is None:
        return "invalid"
    return "apply" if target_rank > current_rank else "noop"


@dataclass(frozen=True, slots=True)
class BindingSnapshot:
    binding_id: int
    owner_user_id: int
    conversation_id: int
    contact_id: int
    contact_e164: str
    preferred_number_id: int | None
    allow_inbound: bool
    allow_outbound: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "owner_user_id": self.owner_user_id,
            "conversation_id": self.conversation_id,
            "contact_id": self.contact_id,
            "contact_e164": self.contact_e164,
            "preferred_number_id": self.preferred_number_id,
            "allow_inbound": self.allow_inbound,
            "allow_outbound": self.allow_outbound,
        }
