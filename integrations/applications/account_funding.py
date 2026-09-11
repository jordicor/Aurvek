"""Owner-managed account budgets over the existing application reservation ledger."""
from __future__ import annotations

import json
import math

from admin_audit import log_admin_action
from integrations.embed.models import AppConfig, EmbedError
from integrations.embed.store import _one, _json
from .billing import configure_funding


def get_account_funding_service():
    from integrations.embed.identity import get_embed_store
    return AccountFundingService(get_embed_store())


def _scope(grant):
    if not grant:
        return 'none'
    return ('context' if grant['context_id'] else 'account' if grant['subject'] else
            'beneficiary' if grant['beneficiary_ref'] else 'application')


async def _context_overrides(connection, app_id, subject):
    rows = await (await connection.execute("""SELECT DISTINCT g.* FROM APPLICATION_FUNDING_GRANTS g
        JOIN APPLICATION_CONTEXTS c ON c.app_id=g.app_id AND c.context_id=g.context_id
        JOIN APPLICATION_PARTICIPANTS p ON p.app_id=c.app_id AND p.context_id=c.context_id
        WHERE g.app_id=? AND p.subject=? AND g.subject='' AND g.context_id!=''
          AND g.beneficiary_ref IN ('',COALESCE(c.beneficiary_ref,'')) ORDER BY g.grant_id""", (app_id, subject))).fetchall()
    return [dict(row) for row in rows]


class AccountFundingService:
    def __init__(self, store):
        self.store = store

    async def _authorize(self, connection, app_id, user_id, is_admin):
        if type(user_id) is not int or user_id <= 0 or type(is_admin) is not bool:
            raise EmbedError('application_owner_required', 403)
        row = await _one(connection, 'SELECT config_json FROM EMBED_APPS WHERE app_id=?', (app_id,))
        if not row:
            raise EmbedError('app_unavailable', 404)
        app = AppConfig.model_validate_json(row['config_json'])
        if not is_admin and app.owner_user_id != user_id:
            raise EmbedError('application_owner_required', 403)
        return app

    async def _payer(self, connection, app):
        # The operator chooses this wallet through the existing general grant.
        # An inactive general grant can supply the wallet without funding fallback.
        payer = await _one(connection, """SELECT u.id AS user_id,u.username AS display_name,d.balance,
            a.subject AS local_subject,a.display_name AS local_display_name
            FROM APPLICATION_FUNDING_GRANTS g JOIN USERS u ON u.id=g.payer_user_id
            JOIN USER_DETAILS d ON d.user_id=u.id
            LEFT JOIN EMBED_SUBJECTS s ON s.user_id=u.id
            LEFT JOIN APPLICATION_ACCOUNTS a ON a.subject=s.subject AND a.app_id=g.app_id
            WHERE g.app_id=? AND g.subject='' AND g.context_id='' AND g.beneficiary_ref=''
            AND g.mode='sponsored' AND u.is_enabled=1""", (app.app_id,))
        if payer:
            local_subject = payer.pop('local_subject')
            local_name = payer.pop('local_display_name')
            if local_subject:
                payer['display_name'] = f'{local_name} · {app.display_name}' if local_name else app.display_name
            payer['balance'] = float(payer['balance'] or 0)
        return payer

    async def _accounts(self, connection, app_id):
        rows = await (await connection.execute("""SELECT a.external_user_id,a.subject,a.display_name,
            s.user_id,CASE WHEN m.active=1 AND u.is_enabled=1 THEN 1 ELSE 0 END AS active
            FROM APPLICATION_ACCOUNTS a JOIN EMBED_SUBJECTS s ON s.subject=a.subject
            JOIN EMBED_MEMBERSHIPS m ON m.app_id=a.app_id AND m.subject=a.subject
            JOIN USERS u ON u.id=s.user_id WHERE a.app_id=? ORDER BY a.created_at,a.external_user_id""",
            (app_id,))).fetchall()
        return [dict(row) for row in rows]

    async def state(self, app_id, *, user_id, is_admin):
        async with self.store.connection(readonly=True) as connection:
            app = await self._authorize(connection, app_id, user_id, is_admin)
            payer = await self._payer(connection, app)
            accounts = await self._accounts(connection, app_id)
            grants = [dict(row) for row in await (await connection.execute(
                'SELECT * FROM APPLICATION_FUNDING_GRANTS WHERE app_id=?', (app_id,))).fetchall()]
            participants = [dict(row) for row in await (await connection.execute("""SELECT p.subject,p.context_id,c.beneficiary_ref
                FROM APPLICATION_PARTICIPANTS p JOIN APPLICATION_CONTEXTS c ON c.app_id=p.app_id AND c.context_id=p.context_id
                WHERE p.app_id=?""", (app_id,))).fetchall()]
        result = []
        for account in accounts:
            contexts = {row['context_id']: row['beneficiary_ref'] or '' for row in participants if row['subject'] == account['subject']}
            account_grant = next((grant for grant in grants if grant['subject'] == account['subject']), None)
            contextual = [grant for grant in grants if not grant['subject'] and grant['context_id'] in contexts
                          and grant['beneficiary_ref'] in ('', contexts[grant['context_id']])]
            inherited = [grant for grant in grants if not grant['subject'] and not grant['context_id']
                         and (not grant['beneficiary_ref'] or grant['beneficiary_ref'] in contexts.values())]
            inherited.sort(key=lambda grant: bool(grant['beneficiary_ref']), reverse=True)
            visible = contextual[0] if contextual else account_grant or (inherited[0] if inherited else None)
            result.append({'external_user_id': account['external_user_id'], 'subject': account['subject'],
                'display_name': account['display_name'] or None, 'active': bool(account['active']),
                'funding': {'version': account_grant['version'] if account_grant else 0,
                    'active': bool(visible and visible['active']), 'monthly_limit': visible['monthly_limit'] if visible else None,
                    'payer_user_id': visible['payer_user_id'] if visible else None, 'scope': _scope(visible)}})
        return {'app_id': app_id, 'display_name': app.display_name, 'payer': payer, 'accounts': result}

    async def _prepared_import(self, connection, app_id, account, grant):
        """Only a proven preparation receipt may move its existing budget bag."""
        context_id = grant['context_id']
        if grant['grant_id'] != 'prepared:' + context_id or grant['beneficiary_ref'] or grant['mode'] != 'sponsored':
            raise EmbedError('account_funding_override_conflict', 409)
        others = await _one(connection, """SELECT 1 FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND context_id=? AND subject!=?""", (app_id, context_id, account['subject']))
        if others:
            raise EmbedError('account_funding_override_conflict', 409)
        mixed = await _one(connection, """SELECT 1 FROM APPLICATION_BILLING_OPERATIONS WHERE grant_id=?
            AND (app_id!=? OR COALESCE(json_extract(scope_json,'$.subject'),'')!=?)""",
            (grant['grant_id'], app_id, account['subject']))
        if mixed:
            raise EmbedError('account_funding_override_conflict', 409)
        rows = await (await connection.execute("""SELECT i.operation_id,i.request_json,i.result_json,i.conversation_id
            FROM APPLICATION_INTERVIEW_IMPORTS i JOIN APPLICATION_CONVERSATIONS c ON c.conversation_id=i.conversation_id
            WHERE i.app_id=? AND i.state='copied' AND c.app_id=i.app_id AND c.subject=? AND c.context_id=?""",
            (app_id, account['subject'], context_id))).fetchall()
        for row in rows:
            request = json.loads(row['request_json'])
            receipt = json.loads(row['result_json']).get('receipt', {})
            if (request.get('app_id') == app_id and request.get('external_user_id') == account['external_user_id']
                    and request.get('expected_subject') == account['subject'] and request.get('expected_context_id') == context_id
                    and receipt.get('app_id') == app_id and receipt.get('subject') == account['subject']
                    and receipt.get('context_id') == context_id and str(receipt.get('conversation_id')) == str(row['conversation_id'])):
                return row['operation_id']
        raise EmbedError('account_funding_override_conflict', 409)

    async def update(self, app_id, external_user_id, *, monthly_limit, active, expected_version,
                     user_id, is_admin, request=None):
        if (type(active) is not bool or type(expected_version) is not int or expected_version < 0
                or (monthly_limit is not None and (type(monthly_limit) not in (int, float)
                    or not math.isfinite(monthly_limit) or monthly_limit < 0))):
            raise EmbedError('invalid_funding', 400)
        async with self.store.transaction() as connection:
            app = await self._authorize(connection, app_id, user_id, is_admin)
            payer = await self._payer(connection, app)
            if payer is None:
                raise EmbedError('application_payer_unconfigured', 409)
            account = next((row for row in await self._accounts(connection, app_id) if row['external_user_id'] == external_user_id), None)
            if not account or not account['active']:
                raise EmbedError('account_unavailable', 409)
            existing = await _one(connection, 'SELECT * FROM APPLICATION_FUNDING_GRANTS WHERE app_id=? AND subject=?', (app_id, account['subject']))
            if (existing['version'] if existing else 0) != expected_version:
                raise EmbedError('account_funding_changed', 409)
            overrides = await _context_overrides(connection, app_id, account['subject'])
            import_operation_id = None
            before = existing
            if overrides:
                if existing or len(overrides) != 1:
                    raise EmbedError('account_funding_override_conflict', 409)
                before = overrides[0]
                import_operation_id = await self._prepared_import(connection, app_id, account, before)
                await connection.execute("""UPDATE APPLICATION_FUNDING_GRANTS SET context_id='',subject=?
                    WHERE grant_id=? AND app_id=?""", (account['subject'], before['grant_id'], app_id))
            grant = await configure_funding(app_id, payer_user_id=payer['user_id'], monthly_limit=monthly_limit,
                subject=account['subject'], mode='sponsored', active=active, connection=connection)
            await log_admin_action(admin_id=user_id, action_type='application_account_funding', request=request,
                target_user_id=account['user_id'], target_resource_type='application_account',
                details=_json({'app_id': app_id, 'subject': account['subject'], 'before': before, 'after': grant,
                               'source_import_operation_id': import_operation_id}), connection=connection)
        return await self.state(app_id, user_id=user_id, is_admin=is_admin)
