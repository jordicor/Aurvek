"""Transactional route fencing and retryable native activity cleanup.

Only durable app conversation bindings select routes for local revocation. A
global phone replacement additionally unlinks native Telegram and every former
phone route. Post-commit work uses a captured snapshot so retries do not discover
and terminate newly authorized provider calls or delegated sessions.
"""
from __future__ import annotations

import json
import sys

from integrations.embed.models import EmbedError
from integrations.embed.store import _json, _one


async def _has_table(connection, name):
    return bool(await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? COLLATE NOCASE", (name,)))


async def fence_account_routes(connection, *, user_id, version, app_id=None, subject=None):
    """Caller owns transaction; no providers, imports of app, or session commits."""
    global_contact = app_id is None
    key = f'contact:{user_id}:{version}' if global_contact else f'membership:{app_id}:{subject}:{version}'
    if global_contact:
        cursor = await connection.execute('SELECT id FROM CONVERSATIONS WHERE user_id=?', (user_id,))
        conversations = [int(row[0]) for row in await cursor.fetchall()]
        cursor = await connection.execute('SELECT session_id FROM EMBED_DELEGATED_SESSIONS WHERE user_id=?', (user_id,))
        columns = {row[1] for row in await (await connection.execute('PRAGMA table_info(USERS)')).fetchall()}
        if 'telegram_chat_id' in columns:
            await connection.execute('UPDATE USERS SET telegram_chat_id=NULL WHERE id=?', (user_id,))
    else:
        cursor = await connection.execute('''SELECT conversation_id FROM APPLICATION_CONVERSATIONS
            WHERE app_id=? AND subject=? AND user_id=? UNION SELECT conversation_id FROM EMBED_INTERVIEWS
            WHERE app_id=? AND subject=? AND user_id=? AND conversation_id IS NOT NULL''',
            (app_id, subject, user_id, app_id, subject, user_id))
        conversations = [int(row[0]) for row in await cursor.fetchall()]
        cursor = await connection.execute('SELECT session_id FROM EMBED_DELEGATED_SESSIONS WHERE app_id=? AND subject=? AND user_id=?',
                                           (app_id, subject, user_id))
    sessions = [row[0] for row in await cursor.fetchall()]
    if await _has_table(connection, 'APPLICATION_CHANNEL_LINKS'):
        if global_contact:
            # These independently proven identities do not migrate to a new
            # phone by matching profile text; they must be linked again.
            predicate = 'subject IN (SELECT subject FROM EMBED_SUBJECTS WHERE user_id=?)'
            parameters = (user_id,)
        else:
            predicate = 'app_id=? AND subject=?'
            parameters = (app_id, subject)
        await connection.execute(f'''UPDATE APPLICATION_CHANNEL_ROUTES SET active=0,version=version+1
            WHERE link_id IN (SELECT link_id FROM APPLICATION_CHANNEL_LINKS WHERE {predicate})''', parameters)
        await connection.execute(f'''UPDATE APPLICATION_CHANNEL_LINKS SET active=0,version=version+1,
            proof_secret_hash=NULL WHERE {predicate}''', parameters)
        await connection.execute(f'DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE {predicate}', parameters)
    calls = []
    if await _has_table(connection, 'PHONE_CONVERSATION_BINDINGS'):
        from integrations.telephony.repository import TelephonyRepository
        from integrations.telephony.schemas import CALL_INCOMPATIBLE_STATUSES
        repository = TelephonyRepository()
        cursor = await connection.execute('SELECT id,conversation_id FROM PHONE_CONVERSATION_BINDINGS WHERE owner_user_id=? AND active=1', (user_id,))
        bindings = [dict(row) for row in await cursor.fetchall() if global_contact or row['conversation_id'] in conversations]
        # Also retain calls whose binding was already deactivated by an earlier
        # cleanup, but never include another application's conversation.
        cursor = await connection.execute('SELECT * FROM PHONE_CALLS WHERE owner_user_id=? AND deleted_at IS NULL', (user_id,))
        active_statuses = {item.value for item in CALL_INCOMPATIBLE_STATUSES}
        for raw in await cursor.fetchall():
            call = dict(raw)
            if call['status'] not in active_statuses or (not global_contact and call['conversation_id'] not in conversations):
                continue
            if call.get('provider_call_sid'):
                calls.append({key: call[key] for key in ('id', 'status', 'provider_call_sid')})
            elif call.get('provider_request_started_at') or not call.get('job_id'):
                # An ambiguous provider dispatch cannot be claimed closed.
                raise EmbedError('phone_activity_in_progress', 409)
            else:
                job = await _one(connection, 'SELECT * FROM PHONE_CALL_JOBS WHERE id=?', (call['job_id'],))
                if not job:
                    raise EmbedError('phone_activity_in_progress', 409)
                await repository._reject_unstarted_job_for_binding(connection, job)
        for binding in bindings:
            await repository._deactivate_binding(connection, binding['id'])
    from chat.services.stop_signals import stop_signals
    native_generations = [[conversation_id, stop_signals.generation(conversation_id)]
                          for conversation_id in conversations if stop_signals.generation(conversation_id)]
    payload = {'sessions': sessions, 'conversations': conversations, 'native_generations': native_generations, 'calls': calls,
               'global_contact': global_contact, 'version': version}
    await connection.execute('INSERT OR IGNORE INTO APPLICATION_ACCOUNT_CLEANUP VALUES (?,?,?,?,?,0)',
                             (key, user_id, app_id, subject, _json(payload)))


async def _stop_conversation(conversation_id, user_id, generation):
    from chat.services.stop_signals import stop_signals
    # Both native providers and GranSabio check this registry. GranSabio then
    # stops its own captured remote session. Calling the generic HTTP stop here
    # would reselect a potentially newer remote session after its database await.
    stop_signals.stop_generation(conversation_id, generation)


async def _hangup_phone(call):
    from integrations.telephony.routes import get_provider_runtime
    from integrations.telephony.schemas import PhoneCallStatus, CALL_INCOMPATIBLE_STATUSES
    runtime = get_provider_runtime()
    current = await runtime.repository.get_call_by_provider_sid(call['provider_call_sid'])
    if current is None or current['id'] != call['id']:
        raise RuntimeError('Phone cleanup lost its durable call identity')
    if current['status'] not in {item.value for item in CALL_INCOMPATIBLE_STATUSES}:
        return {'closed': True}
    await runtime.hangup_durable(current, reason='application_access_changed',
        target_status=PhoneCallStatus.CANCELED, origin='application_accounts')
    current = await runtime.repository.get_call_by_provider_sid(call['provider_call_sid'])
    return {'closed': current is not None and current['status'] not in {item.value for item in CALL_INCOMPATIBLE_STATUSES}}


async def _close_native_sockets(user_id, version):
    """The native managers predate user-indexed sockets; verify their JWT claims."""
    from auth import SESSION_COOKIE_NAME
    from common import SECRET_KEY, decode_jwt_cached
    for module_name in ('app', '__main__', 'chat.routes.voice_io'):
        module = sys.modules.get(module_name)
        manager = getattr(module, 'manager', None) if module else None
        if manager is None:
            continue
        for websocket in list(manager.active_connections):
            token = websocket.cookies.get(SESSION_COOKIE_NAME)
            if not token:
                continue
            try:
                payload = decode_jwt_cached(token, SECRET_KEY)
                actor = payload.get('user_info') or {}
                matches = int(actor['id']) == user_id and int(actor.get('session_version', 0)) < version
            except Exception:
                # A malformed or expired unrelated socket token does not prove
                # membership in this user's cleanup scope.
                matches = False
            if matches:
                manager.disconnect(websocket)
                await websocket.close(code=4401, reason='Session expired')


async def _cleanup(user_id, *, app_id=None, subject=None, store=None,
                   session_ender=None, conversation_stopper=None, phone_hangup=None,
                   socket_closer=None):
    if store is None:
        from integrations.embed.identity import get_embed_store
        store = get_embed_store()
    if session_ender is None:
        from integrations.embed.activity import end_session_activity
        session_ender = end_session_activity
    conversation_stopper = conversation_stopper or _stop_conversation
    phone_hangup = phone_hangup or _hangup_phone
    socket_closer = socket_closer or _close_native_sockets
    async with store.connection(readonly=True) as connection:
        cursor = await connection.execute('''SELECT * FROM APPLICATION_ACCOUNT_CLEANUP
            WHERE user_id=? AND app_id IS ? AND subject IS ? AND completed=0''', (user_id, app_id, subject))
        jobs = [dict(row) for row in await cursor.fetchall()]
    pending = False
    failures = 0
    for job in jobs:
        payload = json.loads(job['payload_json'])
        finished = True
        actions = [(session_ender, (session_id, user_id)) for session_id in payload['sessions']]
        actions += [(conversation_stopper, (conversation_id, user_id, generation))
                    for conversation_id, generation in payload.get('native_generations', [])]
        actions += [(phone_hangup, (call,)) for call in payload['calls']]
        if payload['global_contact']:
            actions.append((socket_closer, (user_id, payload['version'])))
        for action, arguments in actions:
            try:
                result = await action(*arguments)
                if isinstance(result, dict) and result.get('closed') is False:
                    finished = False
            except Exception:
                # Durable job survives and the API reports pending; another
                # retry can reconcile without undoing the database revocation.
                failures += 1
                finished = False
        if finished:
            async with store.transaction() as connection:
                await connection.execute('UPDATE APPLICATION_ACCOUNT_CLEANUP SET completed=1 WHERE cleanup_key=?', (job['cleanup_key'],))
        pending = pending or not finished
    return {'cleanup_pending': pending, 'cleanup_failures': failures}


async def cleanup_contact(user_id: int, **dependencies):
    return await _cleanup(user_id, **dependencies)


async def cleanup_membership(app_id: str, subject: str, user_id: int | None = None, **dependencies):
    if user_id is None:
        store = dependencies.get('store')
        if store is None:
            from integrations.embed.identity import get_embed_store
            store = get_embed_store()
        async with store.connection(readonly=True) as connection:
            row = await _one(connection, 'SELECT user_id FROM EMBED_SUBJECTS WHERE subject=?', (subject,))
            if not row:
                raise EmbedError('not_found', 404)
            user_id = int(row['user_id'])
    return await _cleanup(user_id, app_id=app_id, subject=subject, **dependencies)
