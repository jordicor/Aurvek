"""Durable domain primitives for Aurvek's native phone channel."""

from .repository import TelephonyConflictError, TelephonyRepository
from .foreground import (
    ForegroundCommitGuard,
    ForegroundCoordinator,
    TurnForegroundDecision,
    assert_commit_guard_in_transaction,
)

__all__ = [
    "ForegroundCommitGuard",
    "ForegroundCoordinator",
    "TelephonyConflictError",
    "TelephonyRepository",
    "TurnForegroundDecision",
    "assert_commit_guard_in_transaction",
]
