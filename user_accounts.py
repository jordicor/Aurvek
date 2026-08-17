"""Authorization and balance helpers for managed user accounts."""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from typing import Any


MAX_MANAGED_BALANCE = 500.0


class InitialBalanceError(ValueError):
    """Base error for invalid or unfunded initial account balances."""


class InvalidInitialBalanceError(InitialBalanceError):
    """Raised when an initial balance is not finite or is out of range."""


class InsufficientInitialBalanceError(InitialBalanceError):
    """Raised when the funding account cannot cover the initial balance."""


@dataclass(frozen=True)
class UserManagementAccess:
    actor_role: str | None
    target_role: str | None
    has_assigned_relationship: bool

    @property
    def is_admin(self) -> bool:
        return self.actor_role == "admin"

    @property
    def is_creator(self) -> bool:
        return self.actor_role == "user"

    @property
    def can_manage(self) -> bool:
        return self.is_admin or (
            self.is_creator
            and self.target_role == "customer"
            and self.has_assigned_relationship
        )


async def get_live_user_role(conn: Any, user_id: int) -> str | None:
    """Return the role currently stored in the database for ``user_id``."""
    cursor = await conn.execute(
        """
        SELECT LOWER(r.role_name)
        FROM USERS u
        JOIN USER_ROLES r ON r.id = u.role_id
        WHERE u.id = ?
        """,
        (user_id,),
    )
    row = await cursor.fetchone()
    return row[0] if row else None


async def get_user_management_access(
    conn: Any,
    actor_user_id: int,
    target_user_id: int,
) -> UserManagementAccess:
    """Resolve management rights using live roles and an explicit assignment."""
    cursor = await conn.execute(
        """
        SELECT
            LOWER(actor_role.role_name),
            LOWER(target_role.role_name),
            EXISTS (
                SELECT 1
                FROM USER_CREATOR_RELATIONSHIPS relationship
                WHERE relationship.user_id = target.id
                  AND relationship.creator_id = actor.id
                  AND relationship.relationship_type = 'assigned_by'
            )
        FROM USERS actor
        JOIN USER_ROLES actor_role ON actor_role.id = actor.role_id
        JOIN USERS target ON target.id = ?
        JOIN USER_ROLES target_role ON target_role.id = target.role_id
        WHERE actor.id = ?
        """,
        (target_user_id, actor_user_id),
    )
    row = await cursor.fetchone()
    if not row:
        return UserManagementAccess(None, None, False)
    return UserManagementAccess(row[0], row[1], bool(row[2]))


def validate_managed_balance(value: Any) -> float:
    """Normalize a managed balance and reject NaN, infinity and invalid ranges."""
    try:
        balance = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInitialBalanceError("Balance must be a number.") from exc

    if not math.isfinite(balance) or not 0 <= balance <= MAX_MANAGED_BALANCE:
        raise InvalidInitialBalanceError(
            f"Balance must be between $0 and ${MAX_MANAGED_BALANCE:.0f}."
        )
    return balance


async def apply_initial_balance(
    conn: Any,
    *,
    user_id: int,
    amount: Any,
    funder_user_id: int | None = None,
    allow_platform_grant: bool = False,
    granted_by_user_id: int | None = None,
) -> float:
    """Fund a new account inside its creation transaction.

    A creator-funded balance is transferred from an existing wallet. Platform
    credit is only possible when the caller explicitly authorizes an admin
    grant. The new account is expected to start with a zero balance.
    """
    balance = validate_managed_balance(amount)
    if balance == 0:
        return balance

    if bool(funder_user_id) == bool(allow_platform_grant):
        raise InitialBalanceError(
            "A positive initial balance requires exactly one funding source."
        )
    if funder_user_id == user_id:
        raise InitialBalanceError("An account cannot fund its own initial balance.")

    # The shared reference correlates the two ledger rows without embedding either
    # party's account id. It remains opaque even if one account is later erased.
    reference_id = f"initial_balance_v2_{secrets.token_hex(16)}"

    if allow_platform_grant:
        grant_role_cursor = await conn.execute(
            """
            SELECT 1
            FROM USERS grantor
            JOIN USER_ROLES grantor_role ON grantor_role.id = grantor.role_id
            WHERE grantor.id = ?
              AND LOWER(grantor_role.role_name) = 'admin'
            """,
            (granted_by_user_id,),
        )
        if not await grant_role_cursor.fetchone():
            raise InitialBalanceError(
                "Only a current administrator can grant platform credit."
            )

        update_cursor = await conn.execute(
            """
            UPDATE USER_DETAILS
            SET balance = ?
            WHERE user_id = ?
            RETURNING balance
            """,
            (balance, user_id),
        )
        if not await update_cursor.fetchone():
            raise InitialBalanceError("The new account wallet does not exist.")

        await conn.execute(
            """
            INSERT INTO TRANSACTIONS
                (user_id, type, amount, balance_before, balance_after,
                 description, reference_id)
            VALUES (?, 'balance_credit', ?, 0, ?, ?, ?)
            """,
            (
                user_id,
                balance,
                balance,
                "Platform initial balance credit",
                reference_id,
            ),
        )
        return balance

    debit_cursor = await conn.execute(
        """
        UPDATE USER_DETAILS
        SET balance = COALESCE(balance, 0) - ?
        WHERE user_id = ?
          AND COALESCE(balance, 0) >= ?
        RETURNING balance
        """,
        (balance, funder_user_id, balance),
    )
    debit_row = await debit_cursor.fetchone()
    if not debit_row:
        raise InsufficientInitialBalanceError(
            "The funding account has insufficient balance."
        )

    funder_balance_after = float(debit_row[0])
    credit_cursor = await conn.execute(
        """
        UPDATE USER_DETAILS
        SET balance = ?
        WHERE user_id = ?
        RETURNING balance
        """,
        (balance, user_id),
    )
    if not await credit_cursor.fetchone():
        raise InitialBalanceError("The new account wallet does not exist.")

    await conn.execute(
        """
        INSERT INTO TRANSACTIONS
            (user_id, type, amount, balance_before, balance_after,
             description, reference_id)
        VALUES (?, 'balance_transfer_out', ?, ?, ?, ?, ?)
        """,
        (
            funder_user_id,
            balance,
            funder_balance_after + balance,
            funder_balance_after,
            "Balance transfer sent",
            reference_id,
        ),
    )
    await conn.execute(
        """
        INSERT INTO TRANSACTIONS
            (user_id, type, amount, balance_before, balance_after,
             description, reference_id)
        VALUES (?, 'balance_transfer_in', ?, 0, ?, ?, ?)
        """,
        (
            user_id,
            balance,
            balance,
            "Balance transfer received",
            reference_id,
        ),
    )
    return balance
