"""Application account controls using Aurvek's existing phone services."""
from __future__ import annotations

import os

from integrations.embed.models import EmbedError
from integrations.embed.store import _one
from .accounts import ApplicationAccountService
from .channels import ApplicationChannelService
from .phone import phone_scope, prepare_inbound_binding
from .service import ApplicationService


class ApplicationPhoneControls:
    def __init__(self, store, *, repository=None, user_service=None, voice_client_factory=None):
        from integrations.telephony.repository import TelephonyRepository
        from integrations.telephony.user_service import UserPhoneService
        self.store = store
        self.repository = repository or TelephonyRepository(store.connection)
        self.user_service = user_service or UserPhoneService(self.repository, connection_factory=store.connection)
        self.voice_client_factory = voice_client_factory

    async def account_conversation(self, app_id, external_user_id, conversation_id, *, reductive=False):
        """Cancellation retains ownership after access expires, without exposing history."""
        async with self.store.connection(readonly=True) as connection:
            await self.store._app(connection, app_id)
            account = await ApplicationAccountService(self.store)._binding(connection, app_id, external_user_id)
            binding = await ApplicationService(self.store).find_binding(connection, conversation_id)
            if (not binding or binding['app_id'] != app_id or binding['subject'] != account['subject']):
                raise EmbedError('not_found', 404)
            if not reductive:
                await self.store._live(connection, app_id, account['user_id'], account['subject'])
                await phone_scope(connection, account['user_id'], conversation_id)
            return account

    async def bind(self, app_id, external_user_id, conversation_id, link_id):
        channels = ApplicationChannelService(self.store)
        admission = await channels.authorize_outbound(app_id, external_user_id, link_id, conversation_id)
        async with self.store.transaction() as connection:
            receiver = await _one(connection, 'SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?',
                                  (admission.receiver_id,))
            if receiver['provider_account_id'] != os.getenv('TWILIO_SID', '').strip():
                raise EmbedError('application_phone_outbound_unavailable', 403)
            # One native binding per conversation, and no global contact route.
            await prepare_inbound_binding(connection, self.store.connection, admission,
                caller_e164=admission.provider_identity, called_e164=receiver['receiver_key'],
                account_sid=receiver['provider_account_id'])
        from integrations.telephony.api_routes import _binding_json
        binding = await self.repository.get_active_binding(owner_user_id=admission.scope.user_id,
                                                            conversation_id=conversation_id)
        async with self.store.connection(readonly=True) as connection:
            from .profile import load_profile
            profile = await load_profile(connection, app_id, admission.scope.subject)
        if binding:
            binding = {**dict(binding), 'timezone_name': profile.timezone_name or 'UTC'}
        return {'binding': _binding_json(binding)}

    async def state(self, app_id, external_user_id, conversation_id):
        account = await self.account_conversation(app_id, external_user_id, conversation_id)
        from integrations.telephony.api_routes import _binding_json, _job_json, _call_json
        params = {'owner_user_id': account['user_id'], 'conversation_id': conversation_id}
        binding = await self.repository.get_active_binding(**params)
        async with self.store.connection(readonly=True) as connection:
            from .profile import load_profile
            profile = await load_profile(connection, app_id, account['subject'])
        if binding:
            binding = {**dict(binding), 'timezone_name': profile.timezone_name or 'UTC'}
        return {'binding': _binding_json(binding),
                'jobs': [_job_json(row) for row in await self.repository.list_owned_jobs(**params)],
                'calls': [_call_json(row) for row in await self.repository.list_owned_calls(**params)]}

    async def create(self, app_id, external_user_id, conversation_id, **options):
        account = await self.account_conversation(app_id, external_user_id, conversation_id)
        job, created = await self.user_service.create_call_job(owner_user_id=account['user_id'],
            conversation_id=conversation_id, **options)
        from integrations.telephony.api_routes import _job_json
        return {'created': created, 'job': _job_json(job)}

    async def cancel(self, app_id, external_user_id, conversation_id, job_id):
        account = await self.account_conversation(app_id, external_user_id, conversation_id, reductive=True)
        job = await self.repository.get_owned_job(owner_user_id=account['user_id'], job_id=job_id)
        if int(job['conversation_id']) != conversation_id:
            raise EmbedError('not_found', 404)
        if job['status'] == 'canceled':
            return {'canceled': False, 'state': 'already_canceled'}
        canceled = await self.user_service.outbound_service.cancel_call(owner_user_id=account['user_id'], job_id=job_id)
        if not canceled:
            raise EmbedError('application_phone_job_already_claimed', 409)
        return {'canceled': True, 'state': 'canceled'}

    async def reschedule(self, app_id, external_user_id, conversation_id, job_id, **options):
        account = await self.account_conversation(app_id, external_user_id, conversation_id)
        job = await self.repository.get_owned_job(owner_user_id=account['user_id'], job_id=job_id)
        if int(job['conversation_id']) != conversation_id:
            raise EmbedError('not_found', 404)
        from integrations.telephony.user_service import parse_local_schedule
        instant = parse_local_schedule(options['scheduled_at'], options['timezone_name'], fold=options.get('fold'))
        updated = await self.user_service.outbound_service.reschedule_call(owner_user_id=account['user_id'],
            job_id=job_id, scheduled_at=instant, timezone_name=options['timezone_name'])
        if not updated:
            raise EmbedError('application_phone_job_already_claimed', 409)
        from integrations.telephony.api_routes import _job_json
        return {'rescheduled': True, 'job': _job_json(await self.repository.get_owned_job(
            owner_user_id=account['user_id'], job_id=job_id))}

    async def hangup(self, app_id, external_user_id, conversation_id, call_id):
        account = await self.account_conversation(app_id, external_user_id, conversation_id, reductive=True)
        call = await self.repository.get_owned_call(owner_user_id=account['user_id'], call_id=call_id)
        if int(call['conversation_id']) != conversation_id:
            raise EmbedError('not_found', 404)
        from integrations.telephony.api_routes import hangup_owned_call, _default_voice_client
        return await hangup_owned_call(self.repository, self.voice_client_factory or _default_voice_client,
                                       owner_user_id=account['user_id'], call_id=call_id)
