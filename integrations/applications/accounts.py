"""Application-local identity/profile operations over native accounts and proofs.

Backend authentication is the transport's responsibility. Existing identities
are always revalidated against their delegated session inside each transaction.
Only operator policy permits a backend to create restricted new accounts.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from typing import Annotated

from pydantic import Field, field_validator

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import _json, _one
from .accounts_schema import SCHEMA
from .models import ExternalReference, LocalId, OperationId

Version = Annotated[int, Field(strict=True, ge=1)]


class AccountPolicy(StrictModel):
    allow_provision: bool = Field(default=False, strict=True)
    allow_link: bool = Field(default=False, strict=True)
    allow_membership_management: bool = Field(default=False, strict=True)
    admit_new_accounts: bool = Field(default=False, strict=True)
    llm_id: int = Field(default=1, strict=True, gt=0)
    assistant_ids: list[LocalId] = Field(default_factory=list, max_length=64)
    capabilities: dict[str, Annotated[bool, Field(strict=True)]] = Field(
        default_factory=lambda: {"text": True}, max_length=64)


class AccountOperation(StrictModel):
    operation_id: OperationId
    external_user_id: ExternalReference


class LocalProfile(StrictModel):
    display_name: str | None = Field(default=None, max_length=120)
    preferences: dict = Field(default_factory=dict)
    pending_phone: str | None = Field(default=None, max_length=40)
    pending_email: str | None = Field(default=None, max_length=254)

    @field_validator('preferences')
    @classmethod
    def bounded_preferences(cls, value):
        # Store data, never interpreted capabilities, identity, or native settings.
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError('Preferences must be finite JSON data') from exc
        if len(encoded.encode('utf-8')) > 16384:
            raise ValueError('Preferences exceed 16 KiB')
        if 'conversational_profile' in value:
            from .profile import ConversationalProfile
            value = {**value, 'conversational_profile': ConversationalProfile.model_validate(value['conversational_profile']).model_dump()}
        return value

    @field_validator('pending_phone')
    @classmethod
    def normalized_phone(cls, value):
        if value is None:
            return None
        from phone_verification import normalize_phone_number
        return normalize_phone_number(value)


class ProvisionAccountRequest(AccountOperation, LocalProfile):
    pass


class LinkAccountRequest(AccountOperation):
    pass


class ProfileUpdateRequest(AccountOperation, LocalProfile):
    expected_version: Version


class MembershipUpdateRequest(AccountOperation):
    expected_version: Version
    active: bool = Field(strict=True)


class PhoneChallengeRequest(StrictModel):
    external_user_id: ExternalReference
    phone_number: str = Field(min_length=8, max_length=40)


class PhoneCodeRequest(PhoneChallengeRequest):
    challenge_id: str = Field(min_length=32, max_length=128)
    code: str = Field(min_length=4, max_length=10, pattern=r'^[0-9]+$')


class PhoneUpdateRequest(AccountOperation):
    expected_version: Version
    expected_contact_version: str = Field(min_length=64, max_length=64)
    phone_number: str = Field(min_length=8, max_length=40)
    challenge_id: str = Field(min_length=32, max_length=128)


class ApplicationAccountService:
    def __init__(self, store, *, account_creator=None, twilio_client=None, service_sid=None):
        self.store = store
        self._account_creator = account_creator
        self._twilio = twilio_client
        self._service_sid = service_sid

    async def initialize(self):
        async with self.store.connection() as connection:
            from .contacts import SCHEMA as CONTACT_SCHEMA
            from .profile_frames import SCHEMA as FRAME_SCHEMA
            await connection.executescript(SCHEMA + CONTACT_SCHEMA + FRAME_SCHEMA)
            await connection.commit()

    async def configure_policy(self, app_id: str, policy: AccountPolicy):
        """Operator-only explicit commercial admission, never a backend route."""
        policy = AccountPolicy.model_validate(policy)
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id, active=False)
            await self._prompt_ids(connection, app_id, policy)
            await connection.execute('INSERT INTO APPLICATION_ACCOUNT_POLICIES VALUES (?,?) '
                                     'ON CONFLICT(app_id) DO UPDATE SET config_json=excluded.config_json',
                                     (app_id, policy.model_dump_json()))
            capabilities = await self._admission_capabilities(connection, app_id, policy)
            cursor = await connection.execute('''SELECT s.user_id,a.subject FROM APPLICATION_ACCOUNTS a
                JOIN EMBED_SUBJECTS s USING(subject) JOIN EMBED_MEMBERSHIPS m
                ON m.app_id=a.app_id AND m.subject=a.subject WHERE a.app_id=? AND m.active=1''', (app_id,))
            for row in await cursor.fetchall():
                await self._set_capabilities(connection, app_id, row['subject'], capabilities)
                await self._grant(connection, app_id, row['user_id'], policy)

    async def _admission_capabilities(self, connection, app_id, policy):
        _, config = await self.store._app(connection, app_id, active=False)
        return {key: True for key, enabled in policy.capabilities.items()
                if enabled and config.capabilities.get(key) is True}

    async def _set_capabilities(self, connection, app_id, subject, capabilities):
        member = await _one(connection, 'SELECT capabilities_json FROM EMBED_MEMBERSHIPS WHERE app_id=? AND subject=?',
                            (app_id, subject))
        current = {key: True for key, value in json.loads(member['capabilities_json']).items() if value is True}
        if current == capabilities:
            return
        await connection.execute('''UPDATE EMBED_MEMBERSHIPS SET capabilities_json=?,version=version+1,updated_at=?
            WHERE app_id=? AND subject=?''', (_json(capabilities), self.store.now(), app_id, subject))
        await connection.execute('UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=COALESCE(revoked_at,?) WHERE app_id=? AND subject=?',
                                 (self.store.now(), app_id, subject))

    async def _policy(self, connection, app_id):
        await self.store._app(connection, app_id)
        row = await _one(connection, 'SELECT config_json FROM APPLICATION_ACCOUNT_POLICIES WHERE app_id=?', (app_id,))
        return AccountPolicy.model_validate_json(row['config_json']) if row else AccountPolicy()

    async def _prompt_ids(self, connection, app_id, policy):
        result = set()
        for assistant_id in policy.assistant_ids:
            row = await _one(connection, 'SELECT prompt_id,config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?',
                             (app_id, assistant_id))
            if not row or not json.loads(row['config_json']).get('enabled', True):
                raise EmbedError('assistant_unavailable', 403)
            result.add(row['prompt_id'])
        return result

    async def _identity(self, connection, principal):
        live = await self.store._session_identity(connection, principal.delegated_session_id)
        if (live.app_id, live.subject, live.user_id) != (principal.app_id, principal.subject, principal.user_id):
            raise EmbedError('unauthenticated')
        return live

    async def _binding(self, connection, app_id, external_user_id, principal=None):
        row = await _one(connection, '''SELECT a.*,s.user_id,m.active,m.version AS membership_version
            FROM APPLICATION_ACCOUNTS a JOIN EMBED_SUBJECTS s USING(subject)
            JOIN EMBED_MEMBERSHIPS m ON m.app_id=a.app_id AND m.subject=a.subject
            WHERE a.app_id=? AND a.external_user_id=?''', (app_id, external_user_id))
        if not row or (principal is not None and row['subject'] != principal.subject):
            raise EmbedError('not_found', 404)
        return row

    async def _response(self, connection, row, *, reveal_initial_phone=False):
        from .contacts import contact_state
        contact = await contact_state(connection, row['app_id'], row['subject'])
        return {'external_user_id': row['external_user_id'], 'subject': row['subject'],
                'active': bool(row['active']), 'membership_version': row['membership_version'],
                'version': row['version'], 'display_name': row['display_name'],
                'preferences': json.loads(row['preferences_json']), 'pending_phone': row['pending_phone'],
                'pending_email': row['pending_email'],
                'contact': contact, 'global_contact': None}

    async def _replay(self, connection, app_id, body, kind):
        digest = hashlib.sha256(_json({'kind': kind, 'body': body.model_dump()}).encode()).hexdigest()
        row = await _one(connection, '''SELECT request_hash,result_json FROM APPLICATION_ACCOUNT_OPERATIONS
            WHERE app_id=? AND external_user_id=? AND operation_id=?''',
            (app_id, body.external_user_id, body.operation_id))
        if row and row['request_hash'] != digest:
            raise EmbedError('idempotency_conflict', 409)
        return digest, json.loads(row['result_json']) if row else None

    async def _save(self, connection, app_id, body, digest, result):
        await connection.execute('INSERT INTO APPLICATION_ACCOUNT_OPERATIONS VALUES (?,?,?,?,?)',
                                 (app_id, body.external_user_id, body.operation_id, digest, _json(result)))
        return result

    async def _grant(self, connection, app_id, user_id, policy):
        from marketplace.services.entitlements import grant_prompt_entitlement
        prompt_ids = await self._prompt_ids(connection, app_id, policy)
        await self._revoke_grants(connection, app_id, user_id, keep_prompt_ids=prompt_ids)
        for prompt_id in prompt_ids:
            await grant_prompt_entitlement(connection, user_id=user_id, prompt_id=prompt_id,
                source='application_membership', source_ref_type='application', source_ref_id=app_id,
                reactivate_inactive=True)

    async def _revoke_grants(self, connection, app_id, user_id, *, keep_prompt_ids=()):
        from marketplace.services.entitlements import revoke_entitlement
        cursor = await connection.execute('''SELECT asset_type,asset_id,source_ref_type FROM ENTITLEMENTS
            WHERE user_id=? AND source_ref_type IN ('application','embed_app')
            AND source_ref_id=? AND status='active' ''', (user_id, app_id))
        for entitlement in await cursor.fetchall():
            if entitlement['asset_type'] == 'prompt' and entitlement['asset_id'] in keep_prompt_ids:
                continue
            await revoke_entitlement(connection, user_id=user_id, asset_type=entitlement['asset_type'],
                asset_id=entitlement['asset_id'], source_ref_type=entitlement['source_ref_type'], source_ref_id=app_id)

    async def provision(self, app_id: str, body: ProvisionAccountRequest):
        """Create a new account atomically. Neither contacts nor caller IDs can link."""
        async with self.store.transaction() as connection:
            policy = await self._policy(connection, app_id)
            if not policy.allow_provision:
                raise EmbedError('provisioning_disabled', 403)
            digest, prior = await self._replay(connection, app_id, body, 'provision')
            if prior is not None:
                return prior
            if await _one(connection, 'SELECT 1 FROM APPLICATION_ACCOUNTS WHERE app_id=? AND external_user_id=?', (app_id, body.external_user_id)):
                raise EmbedError('account_already_bound', 409)
            _, config = await self.store._app(connection, app_id)
            capabilities = await self._admission_capabilities(connection, app_id, policy)
            creator = self._account_creator
            if creator is None:
                from account_creation import create_user
                creator = create_user
            user_id = await creator(connection=connection, username='app_' + secrets.token_hex(20),
                prompt_id=config.prompt_id, all_prompts_access=False, public_prompts_access=False,
                llm_id=policy.llm_id, allow_file_upload=capabilities.get('attachments', False),
                allow_image_generation=capabilities.get('image_generation', False),
                balance=0.0, phone=None, email=None, role_name='customer', authentication_mode='magic_link_only',
                initial_password=None, can_change_password=False)
            if not user_id:
                raise EmbedError('service_unavailable', 503)
            subject = secrets.token_urlsafe(24)
            await connection.execute('INSERT INTO EMBED_SUBJECTS VALUES (?,?)', (subject, user_id))
            now = self.store.now()
            await connection.execute('INSERT INTO EMBED_MEMBERSHIPS VALUES (?,?,?,1,?,?,?)',
                (app_id, subject, int(policy.admit_new_accounts), _json(capabilities), now, now))
            await connection.execute('INSERT INTO APPLICATION_ACCOUNTS VALUES (?,?,?,1,?,?,?,?,1,?,?)',
                (app_id, body.external_user_id, subject, body.display_name, _json(body.preferences), body.pending_phone, body.pending_email, now, now))
            if policy.admit_new_accounts:
                await self._grant(connection, app_id, user_id, policy)
            result = await self._response(connection, await self._binding(connection, app_id, body.external_user_id))
            return await self._save(connection, app_id, body, digest, result)

    async def link(self, principal, body: LinkAccountRequest):
        async with self.store.transaction() as connection:
            principal = await self._identity(connection, principal)
            policy = await self._policy(connection, principal.app_id)
            if not policy.allow_link:
                raise EmbedError('linking_disabled', 403)
            digest, prior = await self._replay(connection, principal.app_id, body, 'link')
            existing = await _one(connection, 'SELECT * FROM APPLICATION_ACCOUNTS WHERE app_id=? AND (external_user_id=? OR subject=?)',
                                  (principal.app_id, body.external_user_id, principal.subject))
            if existing and (existing['subject'] != principal.subject or existing['external_user_id'] != body.external_user_id):
                raise EmbedError('account_already_bound', 409)
            if prior is not None:
                return prior
            if not existing:
                now = self.store.now()
                await connection.execute('''INSERT INTO APPLICATION_ACCOUNTS
                    (app_id,external_user_id,subject,created_at,updated_at) VALUES (?,?,?,?,?)''',
                    (principal.app_id, body.external_user_id, principal.subject, now, now))
            await self._set_capabilities(connection, principal.app_id, principal.subject,
                await self._admission_capabilities(connection, principal.app_id, policy))
            await self._grant(connection, principal.app_id, principal.user_id, policy)
            result = await self._response(connection, await self._binding(connection, principal.app_id, body.external_user_id, principal))
            return await self._save(connection, principal.app_id, body, digest, result)

    async def status(self, app_id: str, external_user_id: str):
        async with self.store.connection(readonly=True) as connection:
            await self.store._app(connection, app_id)
            row = await self._binding(connection, app_id, external_user_id)
            return await self._response(connection, row)

    async def account_binding(self, app_id: str, external_user_id: str):
        """Internal authority resolution for authenticated app session assertion."""
        async with self.store.connection(readonly=True) as connection:
            await self.store._app(connection, app_id)
            row = await self._binding(connection, app_id, external_user_id)
            await self.store._live(connection, app_id, row['user_id'], row['subject'])
            return {**dict(row), 'created_by_this_app': bool(row['created_here'])}

    async def update_profile(self, principal, body: ProfileUpdateRequest):
        async with self.store.transaction() as connection:
            principal = await self._identity(connection, principal)
            row = await self._binding(connection, principal.app_id, body.external_user_id, principal)
            digest, prior = await self._replay(connection, principal.app_id, body, 'profile')
            if prior is not None:
                return prior
            if row['version'] != body.expected_version:
                raise EmbedError('version_conflict', 409)
            preferences = json.loads(row['preferences_json'])
            if 'conversational_profile' in preferences and 'conversational_profile' not in body.preferences:
                body = body.model_copy(update={'preferences': {**body.preferences, 'conversational_profile': preferences['conversational_profile']}})
                LocalProfile(preferences=body.preferences)
            await connection.execute('''UPDATE APPLICATION_ACCOUNTS SET display_name=?,preferences_json=?,
                pending_phone=?,pending_email=?,version=version+1,updated_at=? WHERE app_id=? AND external_user_id=?''',
                (body.display_name, _json(body.preferences), body.pending_phone, body.pending_email,
                 self.store.now(), principal.app_id, body.external_user_id))
            result = await self._response(connection, await self._binding(connection, principal.app_id, body.external_user_id))
            return await self._save(connection, principal.app_id, body, digest, result)

    async def set_membership(self, app_id: str, body: MembershipUpdateRequest):
        async with self.store.transaction() as connection:
            policy = await self._policy(connection, app_id)
            if not policy.allow_membership_management:
                raise EmbedError('membership_management_disabled', 403)
            row = await self._binding(connection, app_id, body.external_user_id)
            digest, prior = await self._replay(connection, app_id, body, 'membership')
            if prior is not None:
                return prior
            if row['membership_version'] != body.expected_version:
                raise EmbedError('version_conflict', 409)
            member = await _one(connection, 'SELECT capabilities_json FROM EMBED_MEMBERSHIPS WHERE app_id=? AND subject=?',
                                (app_id, row['subject']))
            current = {key: True for key, value in json.loads(member['capabilities_json']).items() if value is True}
            capabilities = await self._admission_capabilities(connection, app_id, policy) if body.active else current
            changed = bool(row['active']) != body.active or current != capabilities
            if changed:
                await connection.execute('''UPDATE EMBED_MEMBERSHIPS SET active=?,capabilities_json=?,version=version+1,
                    updated_at=? WHERE app_id=? AND subject=?''',
                    (int(body.active), _json(capabilities), self.store.now(), app_id, row['subject']))
            if body.active:
                await self._grant(connection, app_id, row['user_id'], policy)
            else:
                # Source is constrained to this app; purchased and other app grants survive.
                await self._revoke_grants(connection, app_id, row['user_id'])
            if changed:
                await connection.execute('UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=COALESCE(revoked_at,?) WHERE app_id=? AND subject=?',
                                         (self.store.now(), app_id, row['subject']))
            if changed and not body.active:
                from .account_cleanup import fence_account_routes
                await fence_account_routes(connection, user_id=row['user_id'], app_id=app_id,
                                           subject=row['subject'], version=row['membership_version'] + 1)
            result = await self._response(connection, await self._binding(connection, app_id, body.external_user_id))
            return await self._save(connection, app_id, body, digest, result)

    async def _phone_actor(self, principal, body, recent_auth_time):
        # A live application login authorizes only its scoped contact. Native
        # step-up is neither asserted by the host nor needed for this operation.
        async with self.store.connection(readonly=True) as connection:
            principal = await self._identity(connection, principal)
            await self._binding(connection, principal.app_id, body.external_user_id, principal)
            from .contacts import check_phone
            await check_phone(connection, principal.app_id, principal.subject, body.phone_number, replacing=True)
            return principal.user_id

    async def request_phone(self, principal, body: PhoneChallengeRequest, *, request_ip: str, recent_auth_time=None):
        from phone_verification import request_phone_verification, PURPOSE_PROFILE_PHONE_CHANGE
        user_id = await self._phone_actor(principal, body, recent_auth_time)
        result = await request_phone_verification(actor_user_id=user_id, phone_number=body.phone_number,
            purpose=PURPOSE_PROFILE_PHONE_CHANGE, request_ip=request_ip, twilio_client=self._twilio,
            service_sid=self._service_sid, connection_factory=self.store.connection)
        await self._bind_challenge(principal.app_id, body.external_user_id, result.challenge_id, initial=False)
        return {'challenge_id': result.challenge_id, 'status': result.status, 'expires_at': result.expires_at}

    async def verify_phone(self, principal, body: PhoneCodeRequest, *, recent_auth_time=None):
        from phone_verification import verify_phone_code, PURPOSE_PROFILE_PHONE_CHANGE
        user_id = await self._phone_actor(principal, body, recent_auth_time)
        async with self.store.connection(readonly=True) as connection:
            await self._check_challenge(connection, principal.app_id, body, initial=False)
        result = await verify_phone_code(actor_user_id=user_id, challenge_id=body.challenge_id,
            code=body.code, phone_number=body.phone_number, purpose=PURPOSE_PROFILE_PHONE_CHANGE,
            twilio_client=self._twilio, service_sid=self._service_sid, connection_factory=self.store.connection)
        return {'challenge_id': result.challenge_id, 'status': result.status, 'expires_at': result.expires_at}

    async def update_phone(self, principal, body: PhoneUpdateRequest, *, recent_auth_time=None):
        from phone_verification import normalize_phone_number, consume_phone_verification, PURPOSE_PROFILE_PHONE_CHANGE
        phone = normalize_phone_number(body.phone_number)
        async with self.store.transaction() as connection:
            principal = await self._identity(connection, principal)
            row = await self._binding(connection, principal.app_id, body.external_user_id, principal)
            digest, prior = await self._replay(connection, principal.app_id, body, 'phone')
            if prior is not None:
                return prior
            from .contacts import contact_state, activate_phone
            contact = await contact_state(connection, principal.app_id, principal.subject)
            if row['version'] != body.expected_version or contact['version'] != body.expected_contact_version:
                raise EmbedError('version_conflict', 409)
            await self._check_challenge(connection, principal.app_id, body, initial=False)
            await consume_phone_verification(connection, actor_user_id=principal.user_id,
                challenge_id=body.challenge_id, phone_number=phone, purpose=PURPOSE_PROFILE_PHONE_CHANGE)
            await activate_phone(connection, principal.app_id, principal.subject, phone, now=self.store.now(),
                proof_kind='verify', proof_ref=body.challenge_id, replacing=True)
            await connection.execute('''UPDATE APPLICATION_ACCOUNTS SET pending_phone=NULL,version=version+1,updated_at=?
                WHERE app_id=? AND external_user_id=?''', (self.store.now(), principal.app_id, body.external_user_id))
            result = await self._response(connection, await self._binding(connection, principal.app_id, body.external_user_id))
            result['reauthenticate'] = False
            return await self._save(connection, principal.app_id, body, digest, result)

    async def _bind_challenge(self, app_id, external_user_id, challenge_id, *, initial):
        async with self.store.transaction() as connection:
            await connection.execute('INSERT INTO APPLICATION_PHONE_CHALLENGES VALUES (?,?,?,?)',
                                     (challenge_id, app_id, external_user_id, int(initial)))

    async def _check_challenge(self, connection, app_id, body, *, initial):
        if not await _one(connection, '''SELECT 1 FROM APPLICATION_PHONE_CHALLENGES
            WHERE challenge_id=? AND app_id=? AND external_user_id=? AND initial_contact=?''',
            (body.challenge_id, app_id, body.external_user_id, int(initial))):
            raise EmbedError('invalid_phone_proof', 403)

    async def _initial_actor(self, connection, app_id, external_user_id):
        policy = await self._policy(connection, app_id)
        row = await self._binding(connection, app_id, external_user_id)
        from .contacts import contact_state
        contact = await contact_state(connection, app_id, row['subject'])
        user = await _one(connection, 'SELECT is_enabled FROM USERS WHERE id=?', (row['user_id'],))
        if not policy.allow_provision or not row['created_here'] or contact['verified'] or not user or not user['is_enabled']:
            raise EmbedError('initial_contact_not_allowed', 403)
        return row

    async def request_initial_phone(self, app_id: str, body: PhoneChallengeRequest, *, request_ip: str):
        """Bootstrap the scoped contact of an account provisioned by this app."""
        from phone_verification import request_phone_verification, PURPOSE_PROFILE_PHONE_CHANGE
        async with self.store.connection(readonly=True) as connection:
            row = await self._initial_actor(connection, app_id, body.external_user_id)
            from .contacts import check_phone
            await check_phone(connection, app_id, row['subject'], body.phone_number)
        challenge = await request_phone_verification(actor_user_id=row['user_id'], phone_number=body.phone_number,
            purpose=PURPOSE_PROFILE_PHONE_CHANGE, request_ip=request_ip, twilio_client=self._twilio,
            service_sid=self._service_sid, connection_factory=self.store.connection)
        await self._bind_challenge(app_id, body.external_user_id, challenge.challenge_id, initial=True)
        return {'challenge_id': challenge.challenge_id, 'status': challenge.status, 'expires_at': challenge.expires_at}

    async def verify_initial_phone(self, app_id: str, body: PhoneCodeRequest):
        from phone_verification import verify_phone_code, PURPOSE_PROFILE_PHONE_CHANGE
        async with self.store.connection(readonly=True) as connection:
            row = await self._initial_actor(connection, app_id, body.external_user_id)
            await self._check_challenge(connection, app_id, body, initial=True)
        result = await verify_phone_code(actor_user_id=row['user_id'], challenge_id=body.challenge_id,
            code=body.code, phone_number=body.phone_number, purpose=PURPOSE_PROFILE_PHONE_CHANGE,
            twilio_client=self._twilio, service_sid=self._service_sid, connection_factory=self.store.connection)
        return {'challenge_id': result.challenge_id, 'status': result.status, 'expires_at': result.expires_at}

    async def update_initial_phone(self, app_id: str, body: PhoneUpdateRequest):
        from phone_verification import normalize_phone_number, consume_phone_verification, PURPOSE_PROFILE_PHONE_CHANGE
        phone = normalize_phone_number(body.phone_number)
        async with self.store.transaction() as connection:
            policy = await self._policy(connection, app_id)
            if not policy.allow_provision:
                raise EmbedError('provisioning_disabled', 403)
            digest, prior = await self._replay(connection, app_id, body, 'initial_phone')
            if prior is not None:
                return prior
            row = await self._initial_actor(connection, app_id, body.external_user_id)
            from .contacts import contact_state, activate_phone
            contact = await contact_state(connection, app_id, row['subject'])
            if row['version'] != body.expected_version or contact['version'] != body.expected_contact_version:
                raise EmbedError('version_conflict', 409)
            await self._check_challenge(connection, app_id, body, initial=True)
            await consume_phone_verification(connection, actor_user_id=row['user_id'], challenge_id=body.challenge_id,
                phone_number=phone, purpose=PURPOSE_PROFILE_PHONE_CHANGE)
            await activate_phone(connection, app_id, row['subject'], phone, now=self.store.now(),
                proof_kind='verify', proof_ref=body.challenge_id)
            await connection.execute('UPDATE APPLICATION_ACCOUNTS SET pending_phone=NULL,version=version+1,updated_at=? WHERE app_id=? AND external_user_id=?',
                                     (self.store.now(), app_id, body.external_user_id))
            result = await self._response(connection, await self._binding(connection, app_id, body.external_user_id),
                                          reveal_initial_phone=True)
            result['reauthenticate'] = False
            return await self._save(connection, app_id, body, digest, result)
