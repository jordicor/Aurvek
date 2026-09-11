"""Frozen application funding layered on the native reservation ledger.

Funding configuration is an operator service, never an app credential privilege.
Ambient binding only propagates an admitted operation in-process; settlement
loads its immutable attribution from the reservation, including in other workers.
"""
from __future__ import annotations

import json
import math
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from database import get_db_connection
from integrations.applications.models import ApplicationContext
from integrations.applications.billing_schema import initialize_billing_schema
from integrations.embed.models import EmbedError


@dataclass(frozen=True, slots=True)
class ApplicationBillingOperation:
    operation_id: str
    scope: ApplicationContext
    funding_grant_id: str
    funding_version: int
    payer_user_id: int
    mode: str
    budget_month: str
    monthly_limit: float | None


_operation: ContextVar[ApplicationBillingOperation | None] = ContextVar(
    'application_billing_operation', default=None)


def current_application_operation(user_id=None):
    operation = _operation.get()
    if operation is not None and user_id is not None and operation.scope.user_id != int(user_id):
        from billing.usage_reservations import BillingReservationError
        raise BillingReservationError('Application billing user mismatch')
    return operation


@contextmanager
def binding_contextmanager(operation):
    token = _operation.set(operation)
    try:
        yield operation
    finally:
        _operation.reset(token)


async def _one(connection, sql, args=()):
    cursor = await connection.execute(sql, args)
    row = await cursor.fetchone()
    return dict(zip([item[0] for item in cursor.description], row)) if row else None


async def configure_funding(app_id, payer_user_id=None, monthly_limit=None,
                            context_id=None, beneficiary_ref=None, *, mode='sponsored',
                            active=True, subject=None, connection=None):
    """Operator-only grant update. Its stable ID retains budget across revisions."""
    if mode not in {'native', 'sponsored'}:
        raise ValueError('Invalid funding mode')
    if mode == 'sponsored' and (active or payer_user_id is not None) and (
            type(payer_user_id) is not int or payer_user_id <= 0):
        raise ValueError('Sponsored funding requires an exact payer')
    if mode == 'native' and (payer_user_id is not None or monthly_limit is not None):
        raise ValueError('Native funding uses native payer and limits')
    if subject is not None and (not isinstance(subject, str) or not 1 <= len(subject) <= 128):
        raise ValueError('Invalid application account scope')
    if subject and (context_id or beneficiary_ref):
        raise ValueError('Account funding cannot mix context or beneficiary scopes')
    if monthly_limit is not None:
        monthly_limit = float(monthly_limit)
        if not math.isfinite(monthly_limit) or monthly_limit < 0:
            raise ValueError('Invalid application funding limit')
    async def write(db):
        if not await _one(db, 'SELECT app_id FROM EMBED_APPS WHERE app_id=?', (app_id,)):
            raise ValueError('Application not found')
        if payer_user_id is not None and not await _one(db, 'SELECT user_id FROM USER_DETAILS WHERE user_id=?', (payer_user_id,)):
            raise ValueError('Payer not found')
        if context_id and not await _one(db, 'SELECT context_id FROM APPLICATION_CONTEXTS WHERE app_id=? AND context_id=?', (app_id, context_id)):
            raise ValueError('Application context not found')
        if subject and not await _one(db, 'SELECT subject FROM APPLICATION_ACCOUNTS WHERE app_id=? AND subject=?', (app_id, subject)):
            raise ValueError('Application account not found')
        await db.execute("""INSERT INTO APPLICATION_FUNDING_GRANTS
            (grant_id,app_id,context_id,beneficiary_ref,subject,mode,payer_user_id,monthly_limit,active)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(app_id,context_id,beneficiary_ref,subject)
            DO UPDATE SET mode=excluded.mode,payer_user_id=excluded.payer_user_id,
            monthly_limit=excluded.monthly_limit,active=excluded.active,version=version+1""",
            (secrets.token_urlsafe(24), app_id, context_id or '', beneficiary_ref or '', subject or '',
             mode, payer_user_id, monthly_limit, int(active)))
        return await _one(db, '''SELECT * FROM APPLICATION_FUNDING_GRANTS
            WHERE app_id=? AND context_id=? AND beneficiary_ref=? AND subject=?''',
            (app_id, context_id or '', beneficiary_ref or '', subject or ''))
    if connection is not None:
        return await write(connection)
    async with get_db_connection() as db:
        await db.execute('BEGIN IMMEDIATE')
        try:
            grant = await write(db)
            await db.commit()
            return grant
        except BaseException:
            await db.rollback()
            raise


def _scope_json(scope):
    from dataclasses import fields
    return json.dumps({item.name: dict(scope.capabilities) if item.name == 'capabilities'
                       else getattr(scope, item.name) for item in fields(scope)}, sort_keys=True)


def _from_row(row):
    return ApplicationBillingOperation(
        operation_id=row['operation_id'], scope=ApplicationContext(**json.loads(row['scope_json'])),
        funding_grant_id=row['grant_id'], funding_version=int(row['grant_version']),
        payer_user_id=int(row['payer_user_id']), mode=row['mode'],
        budget_month=row['budget_month'], monthly_limit=row['monthly_limit'])


async def _grant(connection, context):
    # A disabled specific grant deliberately shadows an active general grant.
    return await _one(connection, """SELECT * FROM APPLICATION_FUNDING_GRANTS
        WHERE app_id=? AND ((subject=? AND subject!='')
            OR (subject='' AND context_id IN ('',?) AND beneficiary_ref IN ('',?)))
        ORDER BY (context_id!='') DESC,(subject!='') DESC,(beneficiary_ref!='') DESC LIMIT 1""",
        (context.app_id, context.subject, context.context_id, context.beneficiary_ref or ''))


async def admit_application_billing_in_transaction(
    connection, context: ApplicationContext, *, expected_operation=None,
) -> ApplicationBillingOperation:
    """Freeze funding with a channel/job transaction, without acquiring a lock.

    Scheduled work supplies its original operation: dispatch may use the new
    calendar month, but must retain the exact grant revision and payer.
    The caller authorizes the channel before calling and owns commit/rollback.
    """
    if expected_operation is not None:
        await verify_operation(connection, expected_operation, context.user_id)
        await revalidate_application_operation(expected_operation, connection=connection)
        expected_scope = replace(context, payer_user_id=expected_operation.payer_user_id)
        if _scope_json(expected_scope) != _scope_json(expected_operation.scope):
            raise EmbedError('application_scope_changed', 403)
    grant = await _grant(connection, context)
    if not grant or not grant['active']:
        raise EmbedError('application_funding_unavailable', 403)
    payer = grant['payer_user_id']
    if grant['mode'] == 'native':
        member = await _one(connection, 'SELECT COALESCE(billing_account_id,user_id) AS payer FROM USER_DETAILS WHERE user_id=?', (context.user_id,))
        if not member:
            raise EmbedError('application_funding_unavailable', 403)
        payer = member['payer']
    context = replace(context, payer_user_id=int(payer))
    operation = ApplicationBillingOperation(
        operation_id=secrets.token_urlsafe(24), scope=context,
        funding_grant_id=grant['grant_id'], funding_version=grant['version'],
        payer_user_id=int(payer), mode=grant['mode'],
        budget_month=datetime.now(timezone.utc).strftime('%Y-%m'),
        monthly_limit=grant['monthly_limit'])
    await connection.execute("""INSERT INTO APPLICATION_BILLING_OPERATIONS
        (operation_id,grant_id,grant_version,app_id,user_id,payer_user_id,mode,
         budget_month,monthly_limit,scope_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (operation.operation_id, operation.funding_grant_id, operation.funding_version,
         context.app_id, context.user_id, operation.payer_user_id, operation.mode,
         operation.budget_month, operation.monthly_limit, _scope_json(context)))
    return operation


async def load_application_operation(connection, operation_id):
    """Read durable attribution; callers still revalidate before starting work."""
    row = await _one(connection, 'SELECT * FROM APPLICATION_BILLING_OPERATIONS WHERE operation_id=?', (operation_id,))
    if not row:
        raise EmbedError('application_funding_unavailable', 403)
    return _from_row(row)


async def admit_handoff_funding(connection, target: ApplicationContext, llm_id: int):
    """Check native output allowance before a voice transfer, without a hold.

    The next turn still reserves its actual input and audio through the native
    ledger. Callers use their preview or final handoff transaction here.
    """
    from ai_runtime.config import _model_output_cap, limit_live_voice_output
    from billing.usage_reservations import get_variable_billing_rates
    model = await _one(connection, """SELECT input_token_cost,output_token_cost,max_output_tokens
        FROM LLM WHERE id=?""", (llm_id,))
    if model is None:
        raise EmbedError("handoff_model_unavailable", 409)
    rates = await get_variable_billing_rates(user_id=target.user_id, prompt_id=target.prompt_id,
        input_cost_per_million=model["input_token_cost"],
        output_cost_per_million=model["output_token_cost"], byok=False)
    output_cap, _ = _model_output_cap(model["max_output_tokens"])
    output_cap = limit_live_voice_output(output_cap)
    operation = await admit_application_billing_in_transaction(connection, target)
    available = await sponsored_availability(connection, operation)
    if available["available"] <= output_cap * rates.output_per_token:
        raise EmbedError("application_funding_unavailable", 403)
    return operation


async def admit_application_billing(context: ApplicationContext) -> ApplicationBillingOperation:
    async with get_db_connection() as connection:
        await connection.execute('BEGIN IMMEDIATE')
        try:
            operation = await admit_application_billing_in_transaction(connection, context)
            await connection.commit()
            return operation
        except BaseException:
            await connection.rollback()
            raise


async def verify_operation(connection, operation, user_id):
    from billing.usage_reservations import BillingReservationError
    row = await _one(connection, 'SELECT * FROM APPLICATION_BILLING_OPERATIONS WHERE operation_id=? AND user_id=?', (operation.operation_id, int(user_id)))
    if not row or _from_row(row) != operation:
        raise BillingReservationError('Application billing operation mismatch')
    return operation


async def revalidate_application_operation(operation, *, connection=None):
    """Recheck live admission before new work, keeping settlement independent."""
    from integrations.applications.service import ApplicationService
    from integrations.embed.identity import get_embed_store
    if connection is None:
        async with get_db_connection(readonly=True) as owned_connection:
            return await revalidate_application_operation(operation, connection=owned_connection)
    await verify_operation(connection, operation, operation.scope.user_id)
    context = await ApplicationService(get_embed_store()).authorize_runtime_conversation(
        connection, operation.scope.user_id, operation.scope.conversation_id)
    if context is None or _scope_json(replace(context, payer_user_id=operation.payer_user_id)) != _scope_json(operation.scope):
        raise EmbedError('application_scope_changed', 403)
    await _revalidate_funding(connection, operation)


async def _revalidate_funding(connection, operation):
    # Resolve precedence again: a new specific grant can shadow the admitted
    # general grant. Never redirect an existing operation to that new payer.
    # verify_operation intentionally checks only durable identity; historical
    # settlement/refunds must still work after this admission becomes invalid.
    grant = await _grant(connection, operation.scope)
    if not grant or not grant['active']:
        raise EmbedError('application_funding_unavailable', 403)
    payer = grant['payer_user_id']
    if grant['mode'] == 'native':
        member = await _one(connection,
            'SELECT COALESCE(billing_account_id,user_id) AS payer FROM USER_DETAILS WHERE user_id=?',
            (operation.scope.user_id,))
        payer = member['payer'] if member else None
    if (grant['grant_id'] != operation.funding_grant_id
            or int(grant['version']) != operation.funding_version
            or grant['mode'] != operation.mode
            or payer != operation.payer_user_id
            or grant['monthly_limit'] != operation.monthly_limit):
        raise EmbedError('application_funding_changed', 403)


async def reservation_operation(connection, reservation_id):
    exists = await _one(connection, "SELECT 1 AS found FROM sqlite_master WHERE type='table' AND name='APPLICATION_BILLING_RESERVATIONS'")
    if not exists:
        return None
    row = await _one(connection, """SELECT o.* FROM APPLICATION_BILLING_OPERATIONS o
        JOIN APPLICATION_BILLING_RESERVATIONS a ON a.operation_id=o.operation_id
        WHERE a.reservation_id=?""", (reservation_id,))
    return _from_row(row) if row else None


async def attach_reservation(connection, reservation_id, operation):
    await verify_operation(connection, operation, operation.scope.user_id)
    await connection.execute('INSERT INTO APPLICATION_BILLING_RESERVATIONS VALUES (?,?)', (reservation_id, operation.operation_id))


async def budget_remaining(connection, operation):
    return await _remaining_grant_budget(connection, operation.funding_grant_id,
        operation.budget_month, operation.monthly_limit)


async def _remaining_grant_budget(connection, grant_id, budget_month, monthly_limit):
    if monthly_limit is None:
        return None
    row = await _one(connection, """SELECT COALESCE(SUM(CASE r.status
        WHEN 'active' THEN r.amount WHEN 'settled' THEN r.settled_amount ELSE 0 END),0) AS spent
        FROM BILLING_USAGE_RESERVATIONS r
        JOIN APPLICATION_BILLING_RESERVATIONS a ON a.reservation_id=r.id
        JOIN APPLICATION_BILLING_OPERATIONS o ON o.operation_id=a.operation_id
        WHERE o.grant_id=? AND o.budget_month=?""", (grant_id, budget_month))
    return max(0.0, float(monthly_limit) - float(row['spent']))


async def application_funding_status(connection, context):
    """Read UI availability for an authorized scope without admitting paid work.

    Native grants retain the existing account UI. Sponsored grants use the same
    payer balance and monthly reservation ledger as runtime admission, exposing
    neither the payer's identity nor their balance to the application user.
    """
    grant = await _grant(connection, context)
    sponsored = bool(grant and grant['mode'] == 'sponsored')
    if not grant or not grant['active']:
        return {'sponsored': sponsored, 'available': False}
    if not sponsored:
        return None
    payer = await _one(connection, 'SELECT balance FROM USER_DETAILS WHERE user_id=?',
                       (grant['payer_user_id'],))
    remaining = await _remaining_grant_budget(connection, grant['grant_id'],
        datetime.now(timezone.utc).strftime('%Y-%m'), grant['monthly_limit'])
    return {'sponsored': True, 'available': bool(payer and float(payer['balance'] or 0) > 0
            and (remaining is None or remaining > 0))}


async def sponsored_availability(connection, operation):
    await verify_operation(connection, operation, operation.scope.user_id)
    payer = await _one(connection, 'SELECT balance FROM USER_DETAILS WHERE user_id=?', (operation.payer_user_id,))
    balance = max(0.0, float(payer['balance'] or 0)) if payer else 0.0
    remaining = await budget_remaining(connection, operation)
    return {'user_id': operation.scope.user_id, 'billing_account_id': operation.payer_user_id,
            'balance': balance, 'available': balance if remaining is None else min(balance, remaining),
            'monthly_remaining': remaining, 'billing_limit_action': 'block'}


async def check_sponsored_budget(connection, operation, amount):
    from billing.usage_reservations import BillingLimitExceededError
    remaining = await budget_remaining(connection, operation)
    if remaining is not None and amount > remaining + 1e-12:
        raise BillingLimitExceededError('Application monthly billing limit reached')
