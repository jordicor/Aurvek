"""Owner-managed Twilio connections; credentials never enter public configuration."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
import json
import re
import secrets

from integrations.embed.models import AppConfig, EmbedError
from integrations.applications.channel_models import ReceiverConfig
from .integration_schema import initialize_twilio_schema
from .twilio_client import AsyncTwilioVoiceClient


@dataclass(frozen=True, slots=True)
class TwilioAccount:
    integration_id: str
    app_id: str
    account_sid: str
    auth_token: str = field(repr=False)
    enabled: bool
    version: int


def _cipher():
    import common
    # Reuse Aurvek's API-key cipher, but never its legacy fallback material.
    if not common.SECRET_KEY or not common.PEPPER:
        raise EmbedError('twilio_encryption_unavailable', 503)
    cipher = common.get_encryption_key()
    if cipher is None:
        raise EmbedError('twilio_encryption_unavailable', 503)
    return cipher


def _account(row):
    try:
        token = _cipher().decrypt(row['auth_token_encrypted'].encode()).decode()
    except EmbedError:
        raise
    except Exception as exc:
        raise EmbedError('twilio_encryption_unavailable', 503) from exc
    return TwilioAccount(row['integration_id'], row['app_id'], row['account_sid'], token,
                         bool(row['enabled']), row['version'])


async def _one(connection, sql, parameters=()):
    row = await (await connection.execute(sql, parameters)).fetchone()
    return dict(row) if row is not None else None


async def resolve_account(integration_id, *, allow_inactive=False, connection=None):
    if connection is None:
        from database import get_db_connection
        async with get_db_connection(readonly=True) as db:
            return await resolve_account(integration_id, allow_inactive=allow_inactive, connection=db)
    row = await _one(connection, 'SELECT * FROM APPLICATION_TWILIO_INTEGRATIONS WHERE integration_id=?', (integration_id,))
    if row is None or (not allow_inactive and not row['enabled']):
        raise EmbedError('twilio_connection_unavailable', 403)
    return _account(row)


async def lookup_receiver_account(receiver_key, account_sid, *, allow_inactive=False, connection=None):
    if connection is None:
        from database import get_db_connection
        async with get_db_connection(readonly=True) as db:
            return await lookup_receiver_account(receiver_key, account_sid, allow_inactive=allow_inactive, connection=db)
    if not await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_CHANNEL_RECEIVERS'"):
        return None
    row = await _one(connection, """SELECT config_json,enabled FROM APPLICATION_CHANNEL_RECEIVERS
        WHERE channel='phone' AND provider='twilio' AND receiver_key=?""", (receiver_key,))
    if row is None:
        return None
    config = ReceiverConfig.model_validate_json(row['config_json'])
    if config.provider_account_id != account_sid or (not allow_inactive and not row['enabled']):
        raise EmbedError('channel_receiver_mismatch', 403)
    if config.twilio_integration_id is None:
        return None
    account = await resolve_account(config.twilio_integration_id, allow_inactive=allow_inactive, connection=connection)
    if (account.app_id, account.account_sid) != (config.app_id, account_sid):
        raise EmbedError('channel_receiver_mismatch', 403)
    return account


async def snapshot_for_number(connection, number_id, app_id=None):
    row = await _one(connection, 'SELECT integration_id,enabled FROM TELEPHONY_NUMBERS WHERE id=?', (number_id,))
    if row is None:
        raise EmbedError('twilio_number_unavailable', 403)
    if row['integration_id'] is None:
        return None
    account = await resolve_account(row['integration_id'], connection=connection)
    if not row['enabled'] or account.app_id != app_id:
        raise EmbedError('twilio_number_unavailable', 403)
    return {'integration_id': account.integration_id, 'account_sid': account.account_sid,
            'credential_version': account.version, 'billing_mode': 'creator_twilio'}


def get_twilio_integration_service():
    from database import get_db_connection
    return TwilioIntegrationService(get_db_connection)


class TwilioIntegrationService:
    def __init__(self, connection_factory, *, client_factory=None):
        self.connection = connection_factory
        self.client_factory = client_factory or (lambda account: AsyncTwilioVoiceClient(account.account_sid, account.auth_token))

    async def initialize(self):
        async with self.connection() as db:
            await initialize_twilio_schema(db)
            await db.commit()

    @asynccontextmanager
    async def transaction(self):
        async with self.connection() as db:
            await db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    async def _owned_app(self, db, app_id, user_id, is_admin):
        row = await _one(db, 'SELECT config_json FROM EMBED_APPS WHERE app_id=?', (app_id,))
        if row is None:
            raise EmbedError('app_unavailable', 404)
        config = AppConfig.model_validate_json(row['config_json'])
        if not is_admin and config.owner_user_id != user_id:
            raise EmbedError('application_owner_required', 403)
        return config

    async def list_apps(self, user_id, *, is_admin=False):
        async with self.connection(readonly=True) as db:
            rows = await (await db.execute('SELECT config_json FROM EMBED_APPS ORDER BY app_id')).fetchall()
        apps = [AppConfig.model_validate_json(row[0]) for row in rows]
        return [{'app_id': app.app_id, 'display_name': app.display_name}
                for app in apps if is_admin or app.owner_user_id == user_id]

    async def _integration(self, db, app_id, expected_version=None):
        row = await _one(db, 'SELECT * FROM APPLICATION_TWILIO_INTEGRATIONS WHERE app_id=?', (app_id,))
        if row is None:
            raise EmbedError('twilio_connection_unavailable', 404)
        if expected_version is not None and row['version'] != expected_version:
            raise EmbedError('twilio_connection_changed', 409)
        return row

    async def state(self, app_id, user_id, *, is_admin=False):
        async with self.connection(readonly=True) as db:
            app = await self._owned_app(db, app_id, user_id, is_admin)
            row = await _one(db, 'SELECT * FROM APPLICATION_TWILIO_INTEGRATIONS WHERE app_id=?', (app_id,))
            result = {'app_id': app_id, 'display_name': app.display_name, 'connection': None, 'numbers': []}
            if row is None:
                return result
            result['connection'] = {key: row[key] for key in ('integration_id', 'account_sid', 'enabled', 'version',
                'validated_at', 'selected_number_id', 'configured_at')}
            result['connection']['previous_voice'] = json.loads(row['previous_voice_json'] or 'null')
            result['connection']['real_call_tested'] = False
            receiver = await _one(db, 'SELECT config_json FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?', (row['receiver_id'],))
            receiver_config = ReceiverConfig.model_validate_json(receiver['config_json']) if receiver else None
            result['connection']['phone_auth'] = receiver_config.phone_auth if receiver_config else 'pin'
            result['connection']['phone_language'] = receiver_config.phone_language if receiver_config else 'en'
            result['connection']['allow_outbound'] = receiver_config.allow_outbound if receiver_config else False
            stored_numbers = {number['provider_number_sid']: dict(number) for number in await (await db.execute("""
                SELECT n.id,n.provider_number_sid,n.integration_id,n.enabled,n.is_outbound_default,
                    EXISTS(SELECT 1 FROM PHONE_CONVERSATION_BINDINGS b WHERE b.preferred_number_id=n.id AND b.active=1) AS has_bindings
                FROM TELEPHONY_NUMBERS n""")).fetchall()}
            for item in json.loads(row['inventory_json']):
                existing = stored_numbers.get(item['sid'])
                available = bool(item['capabilities'].get('voice')) and (not existing or
                    existing['integration_id'] == row['integration_id'] or
                    (existing['integration_id'] is None and not existing['enabled'] and not existing['is_outbound_default'] and not existing['has_bindings']))
                result['numbers'].append({**item, 'available': available,
                    'selected': bool(existing and existing['id'] == row['selected_number_id'])})
            return result

    async def _inventory(self, account):
        from .admin_service import _validate_remote_number_set
        from .user_service import resolve_e164_country
        client = self.client_factory(account)
        try:
            numbers = tuple(await client.list_incoming_phone_numbers())
            _validate_remote_number_set(numbers)
            inventory = []
            for number in numbers:
                canonical, country = resolve_e164_country(number.e164)
                if canonical != number.e164 or (number.iso_country and number.iso_country.upper() != country):
                    raise ValueError('Twilio number country is inconsistent')
                inventory.append({**asdict(number), 'iso_country': country})
            return inventory
        except Exception as exc:
            raise EmbedError('twilio_validation_failed', 503) from exc
        finally:
            await client.close()

    async def connect(self, app_id, user_id, *, account_sid, auth_token, expected_version=None, is_admin=False):
        if not re.fullmatch(r'AC[0-9a-fA-F]{32}', account_sid) or not re.fullmatch(r'[0-9a-fA-F]{32}', auth_token):
            raise EmbedError('twilio_credentials_invalid', 400)
        encrypted = _cipher().encrypt(auth_token.encode()).decode()
        async with self.connection(readonly=True) as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            old = await _one(db, 'SELECT * FROM APPLICATION_TWILIO_INTEGRATIONS WHERE app_id=?', (app_id,))
            if old and (old['account_sid'] != account_sid or old['version'] != expected_version):
                raise EmbedError('twilio_connection_changed', 409)
            integration_id = old['integration_id'] if old else 'twilio-' + secrets.token_hex(16)
        inventory = await self._inventory(TwilioAccount(integration_id, app_id, account_sid, auth_token, False, 1))
        async with self.transaction() as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            current = await _one(db, 'SELECT * FROM APPLICATION_TWILIO_INTEGRATIONS WHERE app_id=?', (app_id,))
            if (current is not None) != (old is not None) or (current and current['version'] != expected_version):
                raise EmbedError('twilio_connection_changed', 409)
            if current:
                await self._disable(db, current)
                await db.execute("""UPDATE APPLICATION_TWILIO_INTEGRATIONS SET auth_token_encrypted=?,version=version+1,
                    inventory_json=?,validated_at=CURRENT_TIMESTAMP,configured_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE integration_id=?""",
                    (encrypted, json.dumps(inventory), integration_id))
            else:
                await db.execute("""INSERT INTO APPLICATION_TWILIO_INTEGRATIONS
                    (integration_id,app_id,account_sid,auth_token_encrypted,inventory_json,validated_at)
                    VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)""", (integration_id, app_id, account_sid, encrypted, json.dumps(inventory)))
        return await self.state(app_id, user_id, is_admin=is_admin)

    async def sync_numbers(self, app_id, user_id, *, expected_version, is_admin=False):
        async with self.connection(readonly=True) as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            row = await self._integration(db, app_id, expected_version)
        inventory = await self._inventory(_account(row))
        async with self.transaction() as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            row = await self._integration(db, app_id, expected_version)
            await db.execute('UPDATE APPLICATION_TWILIO_INTEGRATIONS SET inventory_json=?,validated_at=CURRENT_TIMESTAMP WHERE integration_id=?',
                             (json.dumps(inventory), row['integration_id']))
            present = {item['sid'] for item in inventory if item['capabilities'].get('voice')}
            owned = await (await db.execute('SELECT id,provider_number_sid FROM TELEPHONY_NUMBERS WHERE integration_id=?', (row['integration_id'],))).fetchall()
            for number in owned:
                if number['provider_number_sid'] not in present:
                    await db.execute('UPDATE TELEPHONY_NUMBERS SET enabled=0,inbound_enabled=0,is_outbound_default=0 WHERE id=?', (number['id'],))
                    if number['id'] == row['selected_number_id']:
                        await self._disable(db, row)
                        await db.execute('UPDATE APPLICATION_TWILIO_INTEGRATIONS SET configured_at=NULL WHERE integration_id=?', (row['integration_id'],))
                else:
                    item = next(item for item in inventory if item['sid'] == number['provider_number_sid'])
                    await db.execute("""UPDATE TELEPHONY_NUMBERS SET friendly_name=?,capabilities_json=?,
                        voice_url=?,voice_method=?,status_callback_url=?,status_callback_method=?,voice_application_sid=?,trunk_sid=?,
                        synced_at=(SELECT validated_at FROM APPLICATION_TWILIO_INTEGRATIONS WHERE integration_id=?) WHERE id=?""",
                        (item['friendly_name'], json.dumps(item['capabilities']), item['voice_url'], item['voice_method'],
                         item['status_callback_url'], item['status_callback_method'], item['voice_application_sid'], item['trunk_sid'],
                         row['integration_id'], number['id']))
        return await self.state(app_id, user_id, is_admin=is_admin)

    async def _disable(self, db, row):
        await db.execute('UPDATE APPLICATION_TWILIO_INTEGRATIONS SET enabled=0 WHERE integration_id=?', (row['integration_id'],))
        await db.execute('UPDATE TELEPHONY_NUMBERS SET enabled=0,inbound_enabled=0,is_outbound_default=0 WHERE integration_id=?', (row['integration_id'],))
        if row['receiver_id']:
            await self._receiver(db, row, enabled=False)

    async def _receiver(self, db, row, *, enabled, phone_auth=None, allow_outbound=None):
        old = await _one(db, 'SELECT config_json FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?', (row['receiver_id'],))
        if old is None:
            return
        config = ReceiverConfig.model_validate_json(old['config_json'])
        changes = {'enabled': enabled, 'version': config.version + 1}
        if phone_auth is not None:
            changes['phone_auth'] = phone_auth
        if allow_outbound is not None:
            changes['allow_outbound'] = allow_outbound
        config = config.model_copy(update=changes)
        await db.execute('UPDATE APPLICATION_CHANNEL_RECEIVERS SET enabled=?,version=?,config_json=? WHERE receiver_id=?',
                         (int(enabled), config.version, config.model_dump_json(), config.receiver_id))

    async def disconnect(self, app_id, user_id, *, expected_version, is_admin=False):
        async with self.transaction() as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            row = await self._integration(db, app_id, expected_version)
            await self._disable(db, row)
            await db.execute('UPDATE APPLICATION_TWILIO_INTEGRATIONS SET version=version+1,updated_at=CURRENT_TIMESTAMP WHERE integration_id=?', (row['integration_id'],))
        return await self.state(app_id, user_id, is_admin=is_admin)

    async def activate_number(self, app_id, user_id, *, number_sid, expected_version,
                              confirm_replace=False, phone_auth='pin', phone_language='en', allow_outbound=False, is_admin=False):
        from .security import canonical_twilio_url
        if phone_auth not in {'pin', 'linked_caller'} or phone_language not in {'en', 'es', 'ja', 'fr', 'pt', 'it', 'de'}:
            raise EmbedError('twilio_number_unavailable', 400)
        async with self.connection(readonly=True) as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            row = await self._integration(db, app_id, expected_version)
        account = _account(row)
        inventory = await self._inventory(account)
        number = next((item for item in inventory if item['sid'] == number_sid), None)
        if number is None or not number['capabilities'].get('voice'):
            raise EmbedError('twilio_number_unavailable', 409)
        voice_url = canonical_twilio_url('/webhooks/twilio/voice/inbound')
        callback = canonical_twilio_url('/webhooks/twilio/voice/inbound-status')
        previous = {key: number[key] for key in ('sid', 'e164', 'voice_url', 'voice_method', 'status_callback_url',
                    'status_callback_method', 'voice_application_sid', 'trunk_sid')}
        replaces = bool(number['voice_application_sid'] or number['trunk_sid'] or
            (number['voice_url'] and number['voice_url'] != voice_url) or
            (number['status_callback_url'] and number['status_callback_url'] != callback))
        if replaces and not confirm_replace:
            raise EmbedError('twilio_webhook_confirmation_required', 409)
        # Reserve only this exact number and retain recovery data before any remote write.
        async with self.transaction() as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            row = await self._integration(db, app_id, expected_version)
            existing = await _one(db, 'SELECT * FROM TELEPHONY_NUMBERS WHERE provider_number_sid=? OR e164=?', (number_sid, number['e164']))
            if existing and (existing['provider_number_sid'], existing['e164']) != (number_sid, number['e164']):
                raise EmbedError('twilio_number_in_use', 409)
            if existing and existing['integration_id'] != row['integration_id']:
                if existing['integration_id'] is not None or existing['enabled'] or existing['is_outbound_default']:
                    raise EmbedError('twilio_number_in_use', 409)
                bindings = await _one(db, 'SELECT 1 FROM PHONE_CONVERSATION_BINDINGS WHERE preferred_number_id=? AND active=1', (existing['id'],))
                if bindings:
                    raise EmbedError('twilio_number_in_use', 409)
            receiver = await _one(db, """SELECT * FROM APPLICATION_CHANNEL_RECEIVERS
                WHERE channel='phone' AND provider='twilio' AND receiver_key=?""", (number['e164'],))
            if receiver and (receiver['app_id'] != app_id or receiver['provider_account_id'] != account.account_sid):
                raise EmbedError('twilio_number_in_use', 409)
            await self._disable(db, row)
            await db.execute("""INSERT INTO TELEPHONY_NUMBERS(integration_id,provider_number_sid,e164,friendly_name,
                iso_country,region,capabilities_json,synced_at) VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(provider_number_sid) DO UPDATE SET integration_id=excluded.integration_id,
                capabilities_json=excluded.capabilities_json,synced_at=CURRENT_TIMESTAMP""",
                (row['integration_id'], number_sid, number['e164'], number['friendly_name'], number['iso_country'], number['region'], json.dumps(number['capabilities'])))
            selected = await _one(db, 'SELECT id FROM TELEPHONY_NUMBERS WHERE provider_number_sid=?', (number_sid,))
            receiver_id = receiver['receiver_id'] if receiver else 'twilio-' + secrets.token_hex(12)
            config = ReceiverConfig(receiver_id=receiver_id, app_id=app_id, channel='phone', provider='twilio',
                receiver_key=number['e164'], provider_account_id=account.account_sid, enabled=False,
                phone_auth=phone_auth, phone_language=phone_language, allow_outbound=allow_outbound, twilio_integration_id=row['integration_id'],
                version=receiver['version'] + 1 if receiver else 1)
            await db.execute("""INSERT INTO APPLICATION_CHANNEL_RECEIVERS
                (receiver_id,app_id,channel,provider,provider_account_id,receiver_key,enabled,version,config_json)
                VALUES(?,?,?,?,?,?,0,?,?) ON CONFLICT(receiver_id) DO UPDATE SET enabled=0,
                version=excluded.version,config_json=excluded.config_json""",
                (receiver_id, app_id, 'phone', 'twilio', account.account_sid, number['e164'], config.version, config.model_dump_json()))
            backups = json.loads(row['previous_voice_json'] or '{}')
            backups.setdefault(number_sid, previous)
            await db.execute("""UPDATE APPLICATION_TWILIO_INTEGRATIONS SET selected_number_id=?,receiver_id=?,
                previous_voice_json=?,configured_at=NULL,inventory_json=?,validated_at=CURRENT_TIMESTAMP,
                version=version+1 WHERE integration_id=?""",
                (selected['id'], receiver_id, json.dumps(backups), json.dumps(inventory), row['integration_id']))
        client = self.client_factory(account)
        try:
            applied = await client.update_incoming_number_voice(number_sid, voice_url=voice_url, voice_method='POST',
                status_callback_url=callback, status_callback_method='POST')
            if (applied.sid != number_sid or applied.e164 != number['e164'] or applied.voice_url != voice_url or
                    applied.voice_method != 'POST' or applied.status_callback_url != callback or
                    applied.status_callback_method != 'POST' or applied.voice_application_sid or applied.trunk_sid):
                raise ValueError('Provider did not confirm configuration')
        except Exception as exc:
            raise EmbedError('twilio_configuration_unconfirmed', 503) from exc
        finally:
            await client.close()
        async with self.transaction() as db:
            await self._owned_app(db, app_id, user_id, is_admin)
            current = await self._integration(db, app_id, expected_version + 1)
            if current['selected_number_id'] != selected['id']:
                raise EmbedError('twilio_connection_changed', 409)
            await db.execute("""UPDATE TELEPHONY_NUMBERS SET enabled=1,inbound_enabled=1,voice_url=?,voice_method='POST',
                status_callback_url=?,status_callback_method='POST',voice_application_sid=NULL,trunk_sid=NULL WHERE id=? AND integration_id=?""",
                (voice_url, callback, selected['id'], row['integration_id']))
            await self._receiver(db, current, enabled=True, phone_auth=phone_auth, allow_outbound=allow_outbound)
            await db.execute("""UPDATE APPLICATION_TWILIO_INTEGRATIONS SET enabled=1,configured_at=CURRENT_TIMESTAMP,
                version=version+1,updated_at=CURRENT_TIMESTAMP WHERE integration_id=?""", (row['integration_id'],))
        return await self.state(app_id, user_id, is_admin=is_admin)
