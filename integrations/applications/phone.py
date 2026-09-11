"""Application attribution around the native phone repository and billing.

Channel proofs select people; native phone services still own contacts, audio
configuration, foreground leases, jobs and provider calls. Snapshots here are
internal data, never a replacement for current channel authorization.
"""
from __future__ import annotations

import json
import hashlib
import os
from contextlib import asynccontextmanager
from functools import wraps

from integrations.embed.models import EmbedError
from integrations.embed.store import _one
from .billing import (
    admit_application_billing_in_transaction, binding_contextmanager,
    current_application_operation, load_application_operation, revalidate_application_operation,
)
from .channel_models import ChannelAdmission


def _services():
    from integrations.embed.identity import get_embed_store
    from .channels import ApplicationChannelService
    from .service import ApplicationService
    store = get_embed_store()
    return ApplicationService(store), ApplicationChannelService(store)


async def registered_receiver(connection, called_e164, account_sid=None):
    if not await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_CHANNEL_RECEIVERS'"):
        return None
    return await _one(connection, '''SELECT * FROM APPLICATION_CHANNEL_RECEIVERS
        WHERE channel='phone' AND provider='twilio' AND receiver_key=?
        AND (? IS NULL OR provider_account_id=?)''', (called_e164, account_sid, account_sid))


async def resolve_phone_entry(connection_factory, params, *, account_sid, context_challenge=None):
    """Called only after the native Twilio signature check."""
    from .channel_models import ChannelResolution, VerifiedChannelEnvelope, ReceiverConfig
    async with connection_factory(readonly=True) as connection:
        receiver = await registered_receiver(connection, str(params.get('To') or ''), account_sid)
    if receiver is None:
        return ChannelResolution('native'), 'en'
    language = ReceiverConfig.model_validate_json(receiver['config_json']).phone_language
    if str(params.get('AccountSid') or '') != account_sid:
        raise EmbedError('channel_provider_mismatch', 403)
    _, channels = _services()
    digits = str(params.get('Digits') or '')
    sid = str(params.get('CallSid') or '')
    event_suffix = hashlib.sha256((digits + ':' + str(context_challenge or '')).encode()).hexdigest()[:24]
    envelope = VerifiedChannelEnvelope('phone', 'twilio', str(params['To']), str(params['From']),
        sid, sid + ':' + event_suffix, provider_account_id=account_sid)
    if digits and context_challenge:
        return await channels.select_context(envelope, context_challenge, digits), language
    if digits:
        return await channels.consume_proof(envelope, digits), language
    return await channels.resolve_inbound(envelope), language


def entry_twiml(resolution, action_url, *, language='en'):
    """Bounded DTMF proof/selection; no history or AI is loaded before proof."""
    from twilio.twiml.voice_response import VoiceResponse
    from urllib.parse import urlencode
    from i18n import Translator
    translator = Translator(language)
    locales = {'en': 'en-US', 'es': 'es-ES', 'ja': 'ja-JP', 'fr': 'fr-FR',
               'pt': 'pt-PT', 'it': 'it-IT', 'de': 'de-DE'}
    response = VoiceResponse()
    if resolution.status == 'context_required':
        action_url += '?' + urlencode({'application_context': resolution.challenge_id})
        text = translator.t('application_twilio.dtmf_choose') + ' ' + ' '.join(
            translator.t('application_twilio.dtmf_press', choice=choice['choice_id'], label=choice['label'])
            for choice in resolution.choices[:9])
    else:
        text = translator.t('application_twilio.dtmf_access_code')
    gather = response.gather(input='dtmf', action=action_url, method='POST',
                             timeout=10, finish_on_key='#', num_digits=10)
    gather.say(text, language=locales[translator.language])
    response.hangup()
    return str(response)


async def prove_outbound_answer(connection_factory, call_id, params, *, account_sid):
    """Prove the scheduled person before exposing history to whoever answered.

    Uses the same personal PIN and sender throttle as inbound calls. It cannot
    switch to another person or choose a different scheduled conversation.
    """
    from .channels import _secret_matches
    from .channel_models import ReceiverConfig, VerifiedChannelEnvelope
    _, channels = _services()
    async with connection_factory() as connection:
        await connection.execute('BEGIN IMMEDIATE')
        try:
            call = await _one(connection, 'SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL', (call_id,))
            if call is None:
                raise EmbedError('not_found', 404)
            operation = await call_operation(connection, call)
            if operation is None:
                await connection.commit()
                return True
            if (call['direction'] != 'outbound' or params.get('AccountSid') != account_sid
                    or params.get('CallSid') != call['provider_call_sid']
                    or params.get('From') != call['from_e164'] or params.get('To') != call['to_e164']):
                raise EmbedError('channel_provider_mismatch', 403)
            config = json.loads(call['config_snapshot_json'])
            attribution = config['_application']
            if attribution.get('answer_verified_sid') == call['provider_call_sid']:
                await connection.commit()
                return True
            admission = ChannelAdmission.from_dict(attribution['channel'])
            receiver = await _one(connection, 'SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?', (admission.receiver_id,))
            settings = ReceiverConfig.model_validate_json(receiver['config_json'])
            peers = await (await connection.execute('''SELECT link_id,proof_secret_hash FROM APPLICATION_CHANNEL_LINKS
                WHERE receiver_id=? AND provider_identity=? AND active=1 LIMIT 65''',
                (admission.receiver_id, admission.provider_identity))).fetchall()
            previous_people = await _one(connection, '''SELECT COUNT(DISTINCT subject) AS count
                FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND provider_identity=?''',
                (admission.receiver_id, admission.provider_identity))
            verified = settings.phone_auth == 'linked_caller' and len(peers) == 1 and previous_people['count'] == 1
            digits = str(params.get('Digits') or '')
            if not verified and digits.isascii() and digits.isdigit() and 6 <= len(digits) <= 10 and len(peers) <= 64:
                envelope = VerifiedChannelEnvelope('phone', 'twilio', call['from_e164'], call['to_e164'],
                    call['provider_call_sid'], call['provider_call_sid'] + ':answer', account_sid)
                if await channels._proof_attempt(connection, receiver, envelope):
                    matches = [row['link_id'] for row in peers if _secret_matches(digits, row['proof_secret_hash'])]
                    verified = matches == [admission.link_id]
            if verified:
                attribution['answer_verified_sid'] = call['provider_call_sid']
                await connection.execute('UPDATE PHONE_CALLS SET config_snapshot_json=? WHERE id=?',
                    (json.dumps(config), call_id))
            await connection.commit()
            return verified
        except BaseException:
            await connection.rollback()
            raise


async def prepare_inbound_binding(connection, connection_factory, admission, *, caller_e164, called_e164, account_sid):
    from integrations.telephony.repository import TelephonyRepository
    _, channels = _services()
    scope = await channels.revalidate(connection, admission, capability='phone')
    receiver = await registered_receiver(connection, called_e164, account_sid)
    if (not receiver or receiver['receiver_id'] != admission.receiver_id
            or admission.provider_identity != caller_e164):
        raise EmbedError('application_phone_receiver_mismatch', 403)
    number = await _one(connection, 'SELECT id FROM TELEPHONY_NUMBERS WHERE e164=? AND enabled=1 AND inbound_enabled=1', (called_e164,))
    if not number:
        raise EmbedError('application_phone_receiver_unavailable', 403)
    repository = TelephonyRepository(connection_factory=connection_factory)
    # Reuse existing metadata, so taking a call does not rename a user's contact.
    contact = await _one(connection, 'SELECT * FROM PHONE_CONTACTS WHERE owner_user_id=? AND e164=? AND active=1',
                         (scope.user_id, caller_e164))
    from .profile import load_profile
    profile = await load_profile(connection, scope.app_id, scope.subject)
    if not contact:
        contact = await repository.create_contact(owner_user_id=scope.user_id, display_name='Application caller',
            e164=caller_e164, timezone_name=profile.timezone_name or 'UTC', connection=connection)
    from .channel_models import ReceiverConfig
    receiver_config = ReceiverConfig.model_validate_json(receiver['config_json'])
    binding = await repository.assign_binding(owner_user_id=scope.user_id,
        conversation_id=scope.conversation_id, contact_id=contact['id'], preferred_number_id=number['id'],
        allow_inbound=True, allow_outbound=receiver_config.allow_outbound,
        preserve_existing_direction_flags=True, application_admission=admission, connection=connection)
    return {**binding, 'contact_e164': caller_e164, 'display_name': contact['display_name'],
            'inbound_number_id': number['id'], 'inbound_number_e164': called_e164}


async def phone_scope(connection, user_id, conversation_id):
    application, _ = _services()
    scope = await application.authorize_runtime_conversation(connection, int(user_id), int(conversation_id))
    # Live phone conversation necessarily receives and produces audio. No
    # silent media downgrade or provider session is started without both grants.
    if scope is not None and not all(scope.capabilities.get(key) for key in ('phone', 'stt', 'tts')):
        raise EmbedError('application_capability_denied', 403)
    return scope


async def validate_binding_assignment(connection, user_id, conversation_id,
                                      e164, number_id, admission):
    scope = await phone_scope(connection, user_id, conversation_id)
    if scope is None:
        if admission is not None:
            raise EmbedError('application_scope_mismatch', 403)
        if number_id is not None:
            from integrations.telephony.integrations import snapshot_for_number
            await snapshot_for_number(connection, number_id, app_id=None)
        return
    if not isinstance(admission, ChannelAdmission):
        raise EmbedError('application_phone_link_required', 403)
    _, channels = _services()
    live = await channels.revalidate(connection, admission, require_current_route=False, capability='phone')
    if (live.conversation_id != int(conversation_id) or live.user_id != int(user_id)
            or admission.provider_identity != e164):
        raise EmbedError('application_scope_mismatch', 403)
    receiver = await _one(connection, 'SELECT channel,receiver_key FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?',
                          (admission.receiver_id,))
    number = await _one(connection, 'SELECT e164 FROM TELEPHONY_NUMBERS WHERE id=? AND enabled=1', (number_id,))
    if not receiver or receiver['channel'] != 'phone' or not number or receiver['receiver_key'] != number['e164']:
        raise EmbedError('application_phone_receiver_mismatch', 403)


async def _binding_authorization(connection, user_id, conversation_id, *, expected_e164=None):
    """Native callers return None; app callers need their verified app link."""
    scope = await phone_scope(connection, user_id, conversation_id)
    if scope is None:
        return None, None, 'en'
    binding = await _one(connection, '''SELECT b.*,c.e164 FROM PHONE_CONVERSATION_BINDINGS b
        JOIN PHONE_CONTACTS c ON c.id=b.contact_id AND c.active=1
        WHERE b.owner_user_id=? AND b.conversation_id=? AND b.active=1''', (user_id, conversation_id))
    if not binding or not binding.get('application_channel_json'):
        raise EmbedError('application_phone_link_required', 403)
    try:
        admission = ChannelAdmission.from_dict(json.loads(binding['application_channel_json']))
    except (ValueError, TypeError, KeyError):
        raise EmbedError('application_phone_link_required', 403) from None
    await validate_binding_assignment(connection, user_id, conversation_id,
        binding['e164'], binding['preferred_number_id'], admission)
    receiver = await _one(connection, 'SELECT provider_account_id,config_json FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?',
                          (admission.receiver_id,))
    from .channel_models import ReceiverConfig
    receiver_config = ReceiverConfig.model_validate_json(receiver['config_json'])
    from integrations.telephony.integrations import snapshot_for_number
    account_snapshot = await snapshot_for_number(connection, binding['preferred_number_id'], app_id=scope.app_id)
    expected_account = (account_snapshot['account_sid'] if account_snapshot
                        else os.getenv('TWILIO_SID', '').strip())
    if (receiver['provider_account_id'] != expected_account or not receiver_config.allow_outbound):
        raise EmbedError('application_phone_outbound_unavailable', 403)
    if expected_e164 is not None and binding['e164'] != expected_e164:
        raise EmbedError('application_phone_link_changed', 403)
    return admission, account_snapshot, receiver_config.phone_language


async def authorize_binding(connection, user_id, conversation_id, *, expected_e164=None):
    admission, _, _ = await _binding_authorization(connection, user_id, conversation_id,
        expected_e164=expected_e164)
    return admission


async def snapshot_job_funding(connection, user_id, conversation_id, config_snapshot, *, number_id=None):
    """Called in the native job TX, after deduplication and before INSERT."""
    if '_twilio' in config_snapshot:
        raise EmbedError('application_scope_mismatch', 403)
    admission, provenance, language = await _binding_authorization(connection, user_id, conversation_id)
    if admission is None:
        if number_id is not None:
            from integrations.telephony.integrations import snapshot_for_number
            await snapshot_for_number(connection, number_id, app_id=None)
        if '_application' in config_snapshot:
            raise EmbedError('application_scope_mismatch', 403)
        return config_snapshot
    operation = await admit_application_billing_in_transaction(connection, admission.scope)
    return {**config_snapshot, **({'_twilio': provenance} if provenance else {}), '_application': {
        'channel': admission.as_dict(), 'billing_operation_id': operation.operation_id,
        'phone_language': language}}


async def job_scope(connection, conversation_id):
    """Only a uniqueness bucket, not permission or user-visible metadata."""
    application, _ = _services()
    binding = await application.find_binding(connection, conversation_id)
    return ((binding['app_id'], binding['subject'], binding['context_id'])
            if binding else ('native',))


async def job_key(connection, user_id, conversation_id, key):
    scope = await phone_scope(connection, user_id, conversation_id)
    if scope is None:
        return key
    material = json.dumps([scope.app_id, scope.subject, scope.context_id, conversation_id, key])
    return 'application:' + hashlib.sha256(material.encode()).hexdigest()


async def call_operation(connection, call, *, revalidate=True):
    """Recover the original payer for a call, including in background workers."""
    config = json.loads(call['config_snapshot_json'])
    attribution = config.get('_application')
    scope = await phone_scope(connection, call['owner_user_id'], call['conversation_id']) if revalidate else None
    if not attribution:
        if scope is not None:
            raise EmbedError('application_phone_link_required', 403)
        return None
    try:
        admission = ChannelAdmission.from_dict(attribution['channel'])
        operation = await load_application_operation(connection, attribution['billing_operation_id'])
    except (TypeError, ValueError, KeyError):
        raise EmbedError('application_scope_mismatch', 403) from None
    if (operation.scope.conversation_id != int(call['conversation_id'])
            or operation.scope.user_id != int(call['owner_user_id'])
            or admission.scope.conversation_id != operation.scope.conversation_id):
        raise EmbedError('application_scope_mismatch', 403)
    if revalidate:
        _, channels = _services()
        await channels.revalidate(connection, admission, require_current_route=False,
                                  require_current_identity=True, capability='phone')
        await revalidate_application_operation(operation, connection=connection)
    return operation


async def dispatch_job_funding(connection, job):
    """Dispatch keeps the scheduled grant, but accounts in the execution month."""
    config = json.loads(job['config_snapshot_json'])
    from integrations.telephony.account_routing import account_for_call
    await account_for_call(job, connection=connection)
    attribution = config.get('_application')
    scope = await phone_scope(connection, job['owner_user_id'], job['conversation_id'])
    if not attribution:
        if scope is not None:
            raise EmbedError('application_phone_link_required', 403)
        return config
    admission = ChannelAdmission.from_dict(attribution['channel'])
    _, channels = _services()
    live = await channels.revalidate(connection, admission, require_current_route=False, capability='phone')
    original = await load_application_operation(connection, attribution['billing_operation_id'])
    if live.conversation_id != int(job['conversation_id']) or live.user_id != int(job['owner_user_id']):
        raise EmbedError('application_scope_mismatch', 403)
    operation = await admit_application_billing_in_transaction(connection, live, expected_operation=original)
    return {**config, '_application': {**attribution, 'billing_operation_id': operation.operation_id}}


@asynccontextmanager
async def phone_billing_context(connection_factory, call_id):
    async with connection_factory(readonly=True) as connection:
        call = await _one(connection, 'SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL', (call_id,))
        if not call:
            raise EmbedError('not_found', 404)
        operation = await call_operation(connection, call)
    ambient = current_application_operation()
    if ambient is not None and (operation is None or ambient.operation_id != operation.operation_id):
        raise EmbedError('application_scope_mismatch', 403)
    with binding_contextmanager(operation):
        yield operation


@asynccontextmanager
async def phone_turn_billing_context(connection_factory, call_id, *, conversation_id):
    """Give each intervention its own scope, handoff identity and settled ledger.

    The outer session binding remains the original call operation. Transport
    and long-lived STT keep that attribution; AI and TTS inherit this turn.
    """
    from integrations.telephony.call_context import active_call
    from .activity import (
        begin_application_admission, bind_application_admission,
        end_application_operation,
    )
    admission = None
    try:
        async with connection_factory() as connection:
            await connection.execute('BEGIN IMMEDIATE')
            try:
                call = await _one(connection, 'SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL', (call_id,))
                if not call:
                    raise EmbedError('not_found', 404)
                current = active_call(call)
                if int(current['conversation_id']) != int(conversation_id):
                    raise EmbedError('application_scope_mismatch', 403)
                original = await call_operation(connection, current)
                operation = None
                if original is not None:
                    admission = begin_application_admission(original.scope)
                    operation = await admit_application_billing_in_transaction(
                        connection, original.scope, expected_operation=original)
                    bind_application_admission(admission, operation)
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise
        with binding_contextmanager(operation):
            yield operation
    finally:
        try:
            pending = admission.pending_handoff if admission is not None else None
            if pending is not None and pending.preparation is not None:
                pending.handled = True
                await pending.preparation.close()
        finally:
            end_application_operation(admission)


def bill_phone_component(function):
    """TTS follows its intervention; duration components retain call funding."""
    @wraps(function)
    async def guarded(self, *, call_id, **kwargs):
        from integrations.telephony.billing import PhoneBillingError
        try:
            operation = current_application_operation()
            if kwargs.get('component_type') == 'tts' and operation is not None:
                from integrations.telephony.call_context import active_call
                async with self._connection_factory(readonly=True) as connection:
                    call = await _one(connection, 'SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL', (call_id,))
                    if (not call or operation.scope.user_id != int(call['owner_user_id'])
                            or operation.scope.conversation_id != int(active_call(call)['conversation_id'])):
                        raise EmbedError('application_scope_mismatch', 403)
                    await revalidate_application_operation(operation, connection=connection)
                return await function(self, call_id=call_id, **kwargs)
            # A transport meter may run inside a turn task. It still belongs to
            # the original call, even when that task now speaks as another AI.
            with binding_contextmanager(None):
                async with phone_billing_context(self._connection_factory, call_id):
                    return await function(self, call_id=call_id, **kwargs)
        except EmbedError as exc:
            raise PhoneBillingError(exc.code) from None
    return guarded


def bill_phone_session(function):
    """Children of this media session inherit the frozen call operation."""
    @wraps(function)
    async def guarded(self, *args, **kwargs):
        if not self.context.call_snapshot.get('_application'):
            return await function(self, *args, **kwargs)
        async with phone_billing_context(self.repository.connection_factory, self.context.call_id):
            return await function(self, *args, **kwargs)
    return guarded


async def authorize_phone_provider(connection_factory, call_id):
    from integrations.telephony.call_context import active_call
    async with connection_factory(readonly=True) as connection:
        call = await _one(connection, 'SELECT * FROM PHONE_CALLS WHERE id=? AND deleted_at IS NULL', (call_id,))
        if call is None:
            raise EmbedError('not_found', 404)
        await call_operation(connection, call)
        if call.get('active_conversation_id') is not None:
            await call_operation(connection, active_call(call))
