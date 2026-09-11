"""Application routing before the existing phone and messaging runtimes.

No transport, provider credential or new chat engine lives here. An authenticated
adapter supplies an envelope; this service resolves and fences its authority.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import hmac
import json
import secrets
from types import SimpleNamespace

from integrations.embed.models import EmbedError
from integrations.embed.store import _digest, _json, _one
from .channel_models import (
    ChannelAdmission, ChannelResolution, LinkInvitationRequest, ReceiverConfig,
    VerifiedChannelEnvelope, validate_address,
)
from .channel_schema import initialize_channel_schema
from .models import OpenConversationRequest
from .service import ApplicationService


def get_application_channel_service():
    from integrations.embed.identity import get_embed_store
    return ApplicationChannelService(get_embed_store())


def _secret_hash(secret):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), 120_000).hex()
    return salt + ":" + digest


def _secret_matches(secret, encoded):
    if not encoded or not isinstance(secret, str) or len(secret) > 128:
        return False
    try:
        salt, digest = encoded.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", secret.encode(), bytes.fromhex(salt), 120_000).hex()
        return hmac.compare_digest(actual, digest)
    except (TypeError, ValueError):
        return False


class ApplicationChannelService:
    def __init__(self, store):
        self.store = store
        self.applications = ApplicationService(store)

    async def initialize(self):
        async with self.store.connection() as connection:
            await initialize_channel_schema(connection)
            await connection.commit()

    async def configure_receiver(self, config: ReceiverConfig):
        """Operator-only: assigning a receiver never adopts native global routes."""
        config = ReceiverConfig.model_validate(config)
        async with self.store.transaction() as connection:
            await self.store._app(connection, config.app_id, active=False)
            if config.twilio_integration_id:
                from integrations.telephony.integrations import resolve_account
                account = await resolve_account(config.twilio_integration_id, allow_inactive=True, connection=connection)
                if (account.app_id, account.account_sid) != (config.app_id, config.provider_account_id):
                    raise EmbedError("channel_receiver_mismatch", 403)
            prior = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (config.receiver_id,))
            if prior and any(prior[key] != getattr(config, key) for key in (
                "app_id", "channel", "provider", "provider_account_id", "receiver_key")):
                raise EmbedError("channel_receiver_immutable", 409)
            collision = await _one(connection, """SELECT receiver_id FROM APPLICATION_CHANNEL_RECEIVERS
                WHERE channel=? AND provider=? AND receiver_key=?""",
                (config.channel, config.provider, config.receiver_key))
            if collision and collision["receiver_id"] != config.receiver_id:
                raise EmbedError("channel_receiver_conflict", 409)
            config = config.model_copy(update={"version": prior["version"] + 1 if prior else 1})
            await connection.execute("""INSERT INTO APPLICATION_CHANNEL_RECEIVERS VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(receiver_id) DO UPDATE SET enabled=excluded.enabled,
                version=excluded.version,config_json=excluded.config_json""",
                (config.receiver_id, config.app_id, config.channel, config.provider,
                 config.provider_account_id, config.receiver_key, int(config.enabled), config.version, config.model_dump_json()))
            if config.enabled and config.channel in {'phone', 'whatsapp'}:
                from .contacts import ensure_phone_adapters
                rows = await (await connection.execute('SELECT subject,phone_number FROM APPLICATION_ACCOUNT_CONTACTS WHERE app_id=?', (config.app_id,))).fetchall()
                for row in rows:
                    await ensure_phone_adapters(connection, config.app_id, row['subject'], row['phone_number'], self.store.now())
            return config

    async def get_receiver(self, receiver_id):
        """Internal credential-reference lookup, before webhook authentication."""
        async with self.store.connection(readonly=True) as connection:
            row = await _one(connection, "SELECT config_json FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (receiver_id,))
            return ReceiverConfig.model_validate_json(row["config_json"]) if row else None

    async def _receiver(self, connection, envelope):
        row = await _one(connection, """SELECT * FROM APPLICATION_CHANNEL_RECEIVERS
            WHERE channel=? AND provider=? AND provider_account_id=? AND receiver_key=?""",
            (envelope.channel, envelope.provider, envelope.provider_account_id, envelope.receiver_key))
        if row is None:
            assigned = await _one(connection, """SELECT 1 FROM APPLICATION_CHANNEL_RECEIVERS
                WHERE channel=? AND provider=? AND receiver_key=?""",
                (envelope.channel, envelope.provider, envelope.receiver_key))
            if assigned:
                raise EmbedError("channel_receiver_mismatch", 403)
            return None
        # A known disabled app/receiver must never fall back into native routing.
        if not row["enabled"]:
            raise EmbedError("channel_receiver_disabled", 403)
        await self.store._app(connection, row["app_id"])
        return row

    async def _link(self, connection, link_id):
        link = await _one(connection, """SELECT l.*,s.user_id FROM APPLICATION_CHANNEL_LINKS l
            JOIN EMBED_SUBJECTS s ON s.subject=l.subject WHERE l.link_id=?""", (link_id,))
        if not link or not link["active"]:
            raise EmbedError("channel_link_inactive", 403)
        await self.store._live(connection, link["app_id"], link["user_id"], link["subject"])
        receiver = await _one(connection, 'SELECT channel FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?', (link['receiver_id'],))
        from .contacts import contact_state
        contact = await contact_state(connection, link['app_id'], link['subject'])
        if not contact['verified'] or (receiver['channel'] in {'phone', 'whatsapp'} and contact['phone_number'] != link['provider_identity']):
            raise EmbedError('phone_proof_required', 403)
        if receiver['channel'] == 'telegram' and not await _one(connection,
                'SELECT 1 FROM APPLICATION_TELEGRAM_CONTACT_PROOFS WHERE link_id=? AND phone_number=?', (link_id, contact['phone_number'])):
            raise EmbedError('phone_proof_required', 403)
        return link

    async def telegram_contact(self, envelope, contact):
        """Only the verified private Bot API sender can assert their own contact."""
        if envelope.channel != 'telegram':
            raise EmbedError('invalid_request', 400)
        async with self.store.transaction() as connection:
            receiver = await self._receiver(connection, envelope)
            if receiver is None:
                return ChannelResolution('native')
            if not await self._proof_attempt(connection, receiver, envelope):
                return ChannelResolution('proof_required')
            if (not isinstance(contact, dict) or type(contact.get('user_id')) is not int
                    or str(contact['user_id']) != envelope.provider_identity):
                return ChannelResolution('proof_required')
            from phone_verification import normalize_phone_number, PhoneVerificationError
            try:
                raw = str(contact.get('phone_number', ''))
                phone = normalize_phone_number(raw if raw.startswith('+') else '+' + raw)
            except (ValueError, PhoneVerificationError):
                return ChannelResolution('proof_required')
            account = await _one(connection, '''SELECT c.subject,s.user_id FROM APPLICATION_ACCOUNT_CONTACTS c
                JOIN EMBED_SUBJECTS s USING(subject) WHERE c.app_id=? AND c.phone_number=?''', (receiver['app_id'], phone))
            if not account:
                return ChannelResolution('proof_required')
            await self.store._live(connection, receiver['app_id'], account['user_id'], account['subject'])
            collision = await _one(connection, '''SELECT 1 FROM APPLICATION_CHANNEL_LINKS
                WHERE receiver_id=? AND provider_identity=? AND subject<>? AND active=1''',
                (receiver['receiver_id'], envelope.provider_identity, account['subject']))
            if collision:
                return ChannelResolution('proof_required')
            old = await _one(connection, 'SELECT link_id FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND provider_identity=? AND subject=?',
                             (receiver['receiver_id'], envelope.provider_identity, account['subject']))
            link_id = old['link_id'] if old else secrets.token_urlsafe(24)
            await connection.execute('''INSERT INTO APPLICATION_CHANNEL_LINKS
                (link_id,receiver_id,app_id,subject,provider_identity,verified_at) VALUES (?,?,?,?,?,?)
                ON CONFLICT(receiver_id,provider_identity,subject) DO UPDATE SET active=1,verified_at=excluded.verified_at''',
                (link_id, receiver['receiver_id'], receiver['app_id'], account['subject'], envelope.provider_identity, self.store.now()))
            await connection.execute('INSERT OR REPLACE INTO APPLICATION_TELEGRAM_CONTACT_PROOFS VALUES (?,?,?)', (link_id, phone, self.store.now()))
            return await self._select_or_open(connection, receiver, envelope, await self._link(connection, link_id))

    async def _snapshot(self, connection, route, receiver=None):
        if not route or not route["active"]:
            raise EmbedError("channel_route_inactive", 403)
        receiver = receiver or await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (route["receiver_id"],))
        if not receiver or not receiver["enabled"]:
            raise EmbedError("channel_receiver_disabled", 403)
        link = await self._link(connection, route["link_id"])
        if (link["receiver_id"] != receiver["receiver_id"] or link["app_id"] != receiver["app_id"]
                or link["provider_identity"] != route["provider_identity"]):
            raise EmbedError("channel_scope_mismatch", 403)
        scope = await self.applications.authorize_runtime_conversation(connection, link["user_id"], route["conversation_id"])
        if scope is None or (scope.app_id, scope.subject) != (link["app_id"], link["subject"]):
            raise EmbedError("channel_scope_mismatch", 403)
        await self.applications._require_unlocked(connection, scope.conversation_id)
        if not scope.capabilities.get("text") or not scope.capabilities.get(receiver["channel"]):
            raise EmbedError("capability_unavailable", 403)
        return ChannelAdmission(receiver["receiver_id"], receiver["version"], link["link_id"], link["version"],
            route["route_id"], route["version"], route["provider_identity"], route["session_key"], scope, route["response_mode"])

    async def revalidate(self, connection, admission, *, require_current_route=True, require_current_identity=False, capability=None):
        """Use the caller's transaction before work; never commit or redirect it.

        Frozen scheduled calls can ignore the current route, but their exact
        receiver, link, subject and conversation remain independently required.
        """
        if isinstance(admission, dict):
            admission = ChannelAdmission.from_dict(admission)
        receiver = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (admission.receiver_id,))
        if not receiver or not receiver["enabled"] or receiver["version"] != admission.receiver_version:
            raise EmbedError("channel_receiver_changed", 403)
        link = await self._link(connection, admission.link_id)
        if (link["version"] != admission.link_version or link["receiver_id"] != admission.receiver_id
                or link["provider_identity"] != admission.provider_identity
                or (link["app_id"], link["subject"], link["user_id"]) != (
                    admission.scope.app_id, admission.scope.subject, admission.scope.user_id)):
            raise EmbedError("channel_link_changed", 403)
        if require_current_route or require_current_identity:
            route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (admission.route_id,))
            if (not route or not route["active"] or route["receiver_id"] != admission.receiver_id
                    or route["link_id"] != admission.link_id or route["session_key"] != admission.session_key
                    or route["provider_identity"] != admission.provider_identity):
                raise EmbedError("channel_identity_changed", 403)
            if require_current_route and (route["version"] != admission.route_version
                    or route["conversation_id"] != admission.scope.conversation_id
                    or route["response_mode"] != admission.response_mode):
                raise EmbedError("channel_route_changed", 409)
        live = await self.applications.authorize_runtime_conversation(connection, admission.scope.user_id, admission.scope.conversation_id)
        if live != replace(admission.scope, payer_user_id=None):
            raise EmbedError("application_scope_changed", 403)
        await self.applications._require_unlocked(connection, live.conversation_id)
        for key in ("text", receiver["channel"], capability):
            if key is not None and not live.capabilities.get(key):
                raise EmbedError("capability_unavailable", 403)
        return live

    async def resolve_runtime(self, admission, *, capability=None, require_current_route=True, require_current_identity=False):
        async with self.store.connection(readonly=True) as connection:
            return await self.revalidate(connection, admission, capability=capability,
                require_current_route=require_current_route, require_current_identity=require_current_identity)

    async def authorize_outbound(self, app_id, external_user_id, link_id, conversation_id):
        """Authorize an app-requested outbound call from an already proven link.

        The backend account assertion is the actor. A conversation ID selects a
        resource; it never supplies the subject, number, prompt or paying account.
        This internal route cannot move an inbound CallSid or messaging session.
        """
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.transaction() as connection:
            link = await self._link(connection, link_id)
            if (link["app_id"], link["subject"], link["user_id"]) != (app_id, account["subject"], account["user_id"]):
                raise EmbedError("channel_scope_mismatch", 403)
            receiver = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (link["receiver_id"],))
            if not receiver or not receiver["enabled"] or receiver["channel"] != "phone":
                raise EmbedError("channel_receiver_unavailable", 403)
            config = ReceiverConfig.model_validate_json(receiver["config_json"])
            if not config.allow_outbound:
                raise EmbedError("channel_outbound_disabled", 403)
            actor = SimpleNamespace(app_id=app_id, subject=account["subject"], user_id=account["user_id"])
            target = await self.applications._authorize_actor_destination(connection, actor, int(conversation_id))
            session_key = "outbound:" + link_id + ":" + target.context_id
            prior = await _one(connection, """SELECT * FROM APPLICATION_CHANNEL_ROUTES
                WHERE receiver_id=? AND provider_identity=? AND session_key=?""",
                (receiver["receiver_id"], link["provider_identity"], session_key))
            if prior and prior["active"] and prior["conversation_id"] == target.conversation_id:
                return await self._snapshot(connection, prior, receiver)
            route_id = prior["route_id"] if prior else secrets.token_urlsafe(24)
            await connection.execute("""INSERT INTO APPLICATION_CHANNEL_ROUTES
                (route_id,receiver_id,link_id,provider_identity,session_key,conversation_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(receiver_id,provider_identity,session_key) DO UPDATE SET
                conversation_id=excluded.conversation_id,active=1,version=version+1,updated_at=excluded.updated_at""",
                (route_id, receiver["receiver_id"], link_id, link["provider_identity"], session_key,
                 target.conversation_id, self.store.now(), self.store.now()))
            route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (route_id,))
            return await self._snapshot(connection, route, receiver)

    async def resolve_inbound(self, envelope: VerifiedChannelEnvelope):
        async with self.store.transaction() as connection:
            receiver = await self._receiver(connection, envelope)
            if receiver is None:
                return ChannelResolution("native")
            await connection.execute("DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE receiver_id=? AND expires_at<=?",
                                     (receiver["receiver_id"], self.store.now()))
            route = await _one(connection, """SELECT * FROM APPLICATION_CHANNEL_ROUTES
                WHERE receiver_id=? AND provider_identity=? AND session_key=?""",
                (receiver["receiver_id"], envelope.provider_identity, envelope.session_key))
            if route:
                # A revoked/closed route does not silently select another person.
                if not route["active"]:
                    return ChannelResolution("proof_required")
                try:
                    return ChannelResolution("admitted", await self._snapshot(connection, route, receiver))
                except EmbedError as error:
                    if error.code == 'phone_proof_required':
                        return ChannelResolution('proof_required')
                    raise
            links = await (await connection.execute("""SELECT link_id FROM APPLICATION_CHANNEL_LINKS
                WHERE receiver_id=? AND provider_identity=? AND active=1""",
                (receiver["receiver_id"], envelope.provider_identity))).fetchall()
            # Caller recognition is an explicit operator tradeoff. Shared
            # numbers always require the person's proof, including former links.
            phone_auto = False
            if envelope.channel == "phone" and len(links) == 1:
                config = ReceiverConfig.model_validate_json(receiver["config_json"])
                identities = await _one(connection, """SELECT COUNT(DISTINCT subject) AS n
                    FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND provider_identity=?""",
                    (receiver["receiver_id"], envelope.provider_identity))
                phone_auto = config.phone_auth == "linked_caller" and identities["n"] == 1
            if (envelope.channel == "phone" and not phone_auto) or len(links) != 1:
                return ChannelResolution("proof_required")
            link = await self._link(connection, links[0][0])
            return await self._select_or_open(connection, receiver, envelope, link)

    async def issue_invitation(self, app_id, request: LinkInvitationRequest):
        """Authenticated app backend, using F2's external-ID binding only."""
        from .accounts import ApplicationAccountService
        request = LinkInvitationRequest.model_validate(request)
        accounts = ApplicationAccountService(self.store)
        account = await accounts.account_binding(app_id, request.external_user_id)
        digest = _digest(_json(request.model_dump(exclude={"access_pin"})))
        async with self.store.transaction() as connection:
            app_row, _ = await self.store._app(connection, app_id)
            receiver = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=? AND app_id=?", (request.receiver_id, app_id))
            if not receiver or not receiver["enabled"]:
                raise EmbedError("channel_receiver_unavailable", 403)
            _, member = await self.store._live(connection, app_id, account["user_id"], account["subject"])
            validate_address(receiver["channel"], receiver["provider"], receiver["receiver_key"], request.provider_identity)
            if receiver['channel'] in {'phone', 'whatsapp'}:
                from .contacts import check_phone
                await check_phone(connection, app_id, account['subject'], request.provider_identity)
            participant = await _one(connection, """SELECT context_id FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND subject=? AND context_ref IS ? AND active=1""",
                (app_id, account["subject"], request.context_ref))
            if request.context_ref is not None and not participant:
                raise EmbedError("not_found", 404)
            prior = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_CHALLENGES WHERE receiver_id=? AND operation_id=?", (request.receiver_id, request.operation_id))
            if prior:
                if prior["request_hash"] != digest:
                    raise EmbedError("operation_conflict", 409)
                return {"challenge_id": prior["challenge_id"], "expires_at": prior["expires_at"], "replayed": True,
                        "code": None, "access_pin": None}
            pin = request.access_pin
            if receiver["channel"] == "phone" and pin is None:
                pin = f"{secrets.randbelow(100_000_000):08d}"
            payload = {"default_context_id": participant["context_id"] if participant else None,
                       "proof_secret_hash": _secret_hash(pin) if pin else None,
                       "receiver_version": receiver["version"], "app_version": app_row["version"],
                       "membership_version": member["version"]}
            challenge_id = secrets.token_urlsafe(24)
            code = f"{secrets.randbelow(100_000_000):08d}"
            now = self.store.now()
            await connection.execute("""INSERT INTO APPLICATION_CHANNEL_CHALLENGES
                (challenge_id,receiver_id,provider_identity,session_key,purpose,app_id,subject,
                 secret_hash,payload_json,expires_at,created_at,operation_id,request_hash)
                VALUES (?,?,?,'*','link',?,?,?,?,?,?,?,?)""",
                (challenge_id, request.receiver_id, request.provider_identity, app_id, account["subject"],
                 _secret_hash(code), _json(payload), now + 900, now, request.operation_id, digest))
            return {"challenge_id": challenge_id, "code": code, "access_pin": pin,
                    "expires_at": now + 900, "replayed": False}

    async def grant_account_context(self, app_id, external_user_id, context_ref, *, assistant_ids=None):
        """The app can prepare its account's context without granting new powers."""
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        if context_ref is not None and (not isinstance(context_ref, str) or not 1 <= len(context_ref) <= 160):
            raise EmbedError("invalid_request", 400)
        async with self.store.transaction() as connection:
            _, app = await self.store._app(connection, app_id)
            _, member = await self.store._live(connection, app_id, account["user_id"], account["subject"])
            member_caps = json.loads(member["capabilities_json"])
            caps = {key: value is True and member_caps.get(key) is True for key, value in app.capabilities.items()}
            if assistant_ids is not None:
                for alias in assistant_ids:
                    row = await _one(connection, "SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?", (app_id, alias))
                    if not row or not json.loads(row["config_json"]).get("enabled", True):
                        raise EmbedError("assistant_unavailable", 403)
            prior = await _one(connection, """SELECT context_id,active,assistant_ids_json,capabilities_json FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND subject=? AND context_ref IS ?""", (app_id, account["subject"], context_ref))
            if prior:
                context = await _one(connection, "SELECT active FROM APPLICATION_CONTEXTS WHERE context_id=?", (prior["context_id"],))
                if not prior["active"] or not context or not context["active"]:
                    raise EmbedError("channel_context_unavailable", 403)
                # An existing operator restriction cannot be widened by asking
                # for the same external reference again through this API.
                prior_caps = json.loads(prior["capabilities_json"])
                caps = {key: value and prior_caps.get(key) is True for key, value in caps.items()}
                prior_assistants = json.loads(prior["assistant_ids_json"]) if prior["assistant_ids_json"] is not None else None
                if prior_assistants is not None:
                    assistant_ids = [item for item in (assistant_ids if assistant_ids is not None else prior_assistants) if item in prior_assistants]
            if not caps.get("text"):
                raise EmbedError("capability_unavailable", 403)
            context_id = prior["context_id"] if prior else secrets.token_urlsafe(24)
            if not prior:
                await connection.execute("INSERT INTO APPLICATION_CONTEXTS VALUES (?,?,NULL,1)", (context_id, app_id))
            await connection.execute("""INSERT INTO APPLICATION_PARTICIPANTS VALUES (?,?,?,?,1,?,?)
                ON CONFLICT(app_id,context_id,subject) DO UPDATE SET
                capabilities_json=excluded.capabilities_json,assistant_ids_json=excluded.assistant_ids_json""",
                (app_id, context_id, account["subject"], context_ref, _json(caps), _json(assistant_ids) if assistant_ids is not None else None))
            return context_id

    async def _proof_attempt(self, connection, receiver, envelope):
        """One durable five-attempt gate per receiver/sender, across CallSids."""
        now = self.store.now()
        await connection.execute("DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE receiver_id=? AND expires_at<=?",
                                 (receiver["receiver_id"], now))
        gate = "rate_" + _digest(_json([receiver["receiver_id"], envelope.provider_identity]))
        old = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_CHALLENGES WHERE challenge_id=?", (gate,))
        if old and old["expires_at"] > now and old["attempts"] >= 5:
            return False
        if not old or old["expires_at"] <= now:
            await connection.execute("""INSERT INTO APPLICATION_CHANNEL_CHALLENGES
                (challenge_id,receiver_id,provider_identity,session_key,purpose,app_id,attempts,expires_at,created_at)
                VALUES (?,?,?,'*','throttle',?,1,?,?) ON CONFLICT(challenge_id) DO UPDATE SET
                attempts=1,expires_at=excluded.expires_at,created_at=excluded.created_at""",
                (gate, receiver["receiver_id"], envelope.provider_identity, receiver["app_id"], now + 300, now))
        else:
            await connection.execute("UPDATE APPLICATION_CHANNEL_CHALLENGES SET attempts=attempts+1 WHERE challenge_id=?", (gate,))
        return True

    async def consume_proof(self, envelope, code, *, challenge_id=None):
        """An invitation binds once; a personal PIN authenticates a new session."""
        if isinstance(code, str) and "." in code and challenge_id is None:
            challenge_id, code = code.split(".", 1)
        if not isinstance(code, str) or not code.isascii() or not code.isdigit() or not 6 <= len(code) <= 10:
            return ChannelResolution("proof_required")
        async with self.store.transaction() as connection:
            receiver = await self._receiver(connection, envelope)
            if receiver is None:
                return ChannelResolution("native")
            if not await self._proof_attempt(connection, receiver, envelope):
                return ChannelResolution("proof_required")
            candidates = await (await connection.execute("""SELECT * FROM APPLICATION_CHANNEL_CHALLENGES
                WHERE receiver_id=? AND provider_identity=? AND purpose='link' AND expires_at>?
                ORDER BY created_at DESC LIMIT 64""", (receiver["receiver_id"], envelope.provider_identity, self.store.now()))).fetchall()
            matched = [dict(row) for row in candidates if (challenge_id is None or row["challenge_id"] == challenge_id)
                       and _secret_matches(code, row["secret_hash"])]
            if len(matched) == 1:
                invitation = matched[0]
                subject = invitation["subject"]
                member = await _one(connection, "SELECT user_id FROM EMBED_SUBJECTS WHERE subject=?", (subject,))
                if not member:
                    raise EmbedError("channel_link_inactive", 403)
                _, membership = await self.store._live(connection, receiver["app_id"], member["user_id"], subject)
                payload = json.loads(invitation["payload_json"])
                app_row, _ = await self.store._app(connection, receiver["app_id"])
                if (payload.get("receiver_version") != receiver["version"]
                        or payload.get("app_version") != app_row["version"]
                        or payload.get("membership_version") != membership["version"]):
                    return ChannelResolution("proof_required")
                if invitation["consumed_at"] is not None:
                    if invitation["consumed_event_id"] != envelope.event_id or invitation["session_key"] != envelope.session_key:
                        return ChannelResolution("proof_required")
                    route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (invitation["result_route_id"],))
                    proof = json.loads(invitation["payload_json"])
                    if (not route or route["link_id"] != invitation["link_id"]
                            or route["version"] != proof.get("result_route_version")):
                        return ChannelResolution("proof_required")
                    admission = await self._snapshot(connection, route, receiver)
                    if admission.scope.subject != invitation["subject"]:
                        return ChannelResolution("proof_required")
                    return ChannelResolution("admitted", admission)
                if receiver['channel'] == 'telegram':
                    # A numeric Bot API ID alone does not prove the canonical phone.
                    return ChannelResolution('proof_required')
                from .contacts import activate_phone
                await activate_phone(connection, receiver['app_id'], subject, envelope.provider_identity,
                    now=self.store.now(), proof_kind='channel:' + receiver['channel'], proof_ref=invitation['challenge_id'])
                old = await _one(connection, """SELECT * FROM APPLICATION_CHANNEL_LINKS
                    WHERE receiver_id=? AND provider_identity=? AND subject=?""", (receiver["receiver_id"], envelope.provider_identity, subject))
                link_id = old["link_id"] if old else secrets.token_urlsafe(24)
                await connection.execute("""INSERT INTO APPLICATION_CHANNEL_LINKS VALUES (?,?,?,?,?,1,1,?,?,?)
                    ON CONFLICT(receiver_id,provider_identity,subject) DO UPDATE SET active=1,version=version+1,
                    default_context_id=excluded.default_context_id,proof_secret_hash=excluded.proof_secret_hash,
                    verified_at=excluded.verified_at""", (link_id, receiver["receiver_id"], receiver["app_id"], subject,
                    envelope.provider_identity, payload.get("default_context_id"), payload.get("proof_secret_hash"), self.store.now()))
                # Adding/replacing a person requires a deliberate fresh choice;
                # another subject's former durable messaging route is not inherited.
                await connection.execute("""UPDATE APPLICATION_CHANNEL_ROUTES SET active=0,version=version+1
                    WHERE receiver_id=? AND provider_identity=?""", (receiver["receiver_id"], envelope.provider_identity))
                link = await self._link(connection, link_id)
                await self._invalidate_session_selections(connection, receiver, envelope)
                result = await self._select_or_open(connection, receiver, envelope, link)
                payload["result_route_version"] = result.admission.route_version if result.admission else None
                await connection.execute("""UPDATE APPLICATION_CHANNEL_CHALLENGES SET consumed_at=?,consumed_event_id=?,
                    result_route_id=?,link_id=?,session_key=?,payload_json=? WHERE challenge_id=?""", (self.store.now(), envelope.event_id,
                    result.admission.route_id if result.admission else None, link_id, envelope.session_key, _json(payload), invitation["challenge_id"]))
                return result
            links = await (await connection.execute("""SELECT link_id,proof_secret_hash FROM APPLICATION_CHANNEL_LINKS
                WHERE receiver_id=? AND provider_identity=? AND active=1""", (receiver["receiver_id"], envelope.provider_identity))).fetchall()
            matched_links = [row["link_id"] for row in links if _secret_matches(code, row["proof_secret_hash"])]
            if len(matched_links) != 1:
                return ChannelResolution("proof_required")
            link = await self._link(connection, matched_links[0])
            await self._invalidate_session_selections(connection, receiver, envelope)
            return await self._select_or_open(connection, receiver, envelope, link)

    async def _invalidate_session_selections(self, connection, receiver, envelope):
        # A fresh person proof replaces pending selections in this session only.
        # A delayed callback must not switch back to a previously proven person.
        await connection.execute("""DELETE FROM APPLICATION_CHANNEL_CHALLENGES
            WHERE receiver_id=? AND provider_identity=? AND session_key=? AND purpose='context'""",
            (receiver["receiver_id"], envelope.provider_identity, envelope.session_key))

    async def _contexts(self, connection, receiver, link):
        from .handoff import resolve_entry_assistant
        actor = SimpleNamespace(app_id=link["app_id"], subject=link["subject"], user_id=link["user_id"])
        rows = await (await connection.execute("""SELECT context_id,context_ref FROM APPLICATION_PARTICIPANTS
            WHERE app_id=? AND subject=? AND active=1 ORDER BY context_id""", (link["app_id"], link["subject"]))).fetchall()
        result = []
        for row in rows:
            try:
                assistant = await resolve_entry_assistant(self.applications, connection, actor, row["context_id"], receiver["channel"],
                                                          required_capability=receiver["channel"])
                _, _, _, caps = await self.applications._scope(connection, link["app_id"], link["subject"], link["user_id"], row["context_id"], assistant)
                if caps.get("text") and caps.get(receiver["channel"]):
                    result.append({**dict(row), "assistant_id": assistant})
            except EmbedError as error:
                if error.code not in {"not_found", "capability_unavailable", "assistant_unavailable"}:
                    raise
        return result

    async def _select_or_open(self, connection, receiver, envelope, link):
        selected = await _one(connection, 'SELECT conversation_id FROM APPLICATION_CHANNEL_DESTINATIONS WHERE link_id=?', (link['link_id'],))
        if selected:
            target = await self.applications._authorize_actor_destination(connection,
                SimpleNamespace(app_id=link['app_id'], subject=link['subject'], user_id=link['user_id']), selected['conversation_id'])
            if not target.capabilities.get(receiver['channel']):
                raise EmbedError('capability_unavailable', 403)
            await connection.execute('''INSERT INTO APPLICATION_CHANNEL_ROUTES
                (route_id,receiver_id,link_id,provider_identity,session_key,conversation_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(receiver_id,provider_identity,session_key)
                DO UPDATE SET link_id=excluded.link_id,conversation_id=excluded.conversation_id,
                active=1,version=version+1,updated_at=excluded.updated_at''',
                (secrets.token_urlsafe(24), receiver['receiver_id'], link['link_id'], envelope.provider_identity,
                 envelope.session_key, target.conversation_id, self.store.now(), self.store.now()))
            route = await _one(connection, 'SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE receiver_id=? AND provider_identity=? AND session_key=?',
                (receiver['receiver_id'], envelope.provider_identity, envelope.session_key))
            return ChannelResolution('admitted', await self._snapshot(connection, route, receiver))
        contexts = await self._contexts(connection, receiver, link)
        preferred = next((row for row in contexts if row["context_id"] == link["default_context_id"]), None)
        if not contexts:
            raise EmbedError("channel_context_unavailable", 403)
        if preferred or len(contexts) == 1:
            return await self._open_route(connection, receiver, envelope, link, preferred or contexts[0])
        challenge_id = secrets.token_urlsafe(24)
        payload = {str(index + 1): row for index, row in enumerate(contexts)}
        _, member = await self.store._live(connection, link["app_id"], link["user_id"], link["subject"])
        app_row, _ = await self.store._app(connection, link["app_id"])
        route = await _one(connection, """SELECT route_id,version FROM APPLICATION_CHANNEL_ROUTES
            WHERE receiver_id=? AND provider_identity=? AND session_key=?""",
            (receiver["receiver_id"], envelope.provider_identity, envelope.session_key))
        payload["_proof"] = {"link_version": link["version"], "receiver_version": receiver["version"],
                             "membership_version": member["version"], "app_version": app_row["version"],
                             "prior_route_id": route["route_id"] if route else None,
                             "prior_route_version": route["version"] if route else None}
        await connection.execute("""INSERT INTO APPLICATION_CHANNEL_CHALLENGES
            (challenge_id,receiver_id,provider_identity,session_key,purpose,app_id,subject,link_id,
             payload_json,expires_at,created_at) VALUES (?,?,?,?,'context',?,?,?,?,?,?)""",
            (challenge_id, receiver["receiver_id"], envelope.provider_identity, envelope.session_key,
             link["app_id"], link["subject"], link["link_id"], _json(payload), self.store.now() + 300, self.store.now()))
        # Context references become visible only after the person's proof.
        choices = tuple({"choice_id": key, "label": value["context_ref"] or "Personal",
                         "display_name": value["context_ref"] or "Personal"} for key, value in payload.items() if key.isdigit())
        return ChannelResolution("context_required", challenge_id=challenge_id, choices=choices)

    async def select_context(self, envelope, challenge_id, choice_id):
        async with self.store.transaction() as connection:
            receiver = await self._receiver(connection, envelope)
            challenge = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_CHALLENGES WHERE challenge_id=?", (challenge_id,))
            if (receiver is None or not challenge or challenge["purpose"] != "context"
                    or challenge["receiver_id"] != receiver["receiver_id"] or challenge["provider_identity"] != envelope.provider_identity
                    or challenge["session_key"] != envelope.session_key or challenge["expires_at"] <= self.store.now()):
                raise EmbedError("channel_selection_invalid", 403)
            payload = json.loads(challenge["payload_json"])
            link = await self._link(connection, challenge["link_id"])
            _, member = await self.store._live(connection, link["app_id"], link["user_id"], link["subject"])
            app_row, _ = await self.store._app(connection, link["app_id"])
            proof = payload.get("_proof", {})
            if (proof.get("link_version") != link["version"] or proof.get("receiver_version") != receiver["version"]
                    or proof.get("membership_version") != member["version"] or proof.get("app_version") != app_row["version"]):
                raise EmbedError("channel_selection_invalid", 403)
            if challenge["consumed_at"] is not None:
                if challenge["consumed_event_id"] != envelope.event_id:
                    raise EmbedError("channel_selection_used", 409)
                route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (challenge["result_route_id"],))
                if (not route or route["link_id"] != challenge["link_id"]
                        or route["version"] != proof.get("result_route_version")):
                    raise EmbedError("channel_route_changed", 409)
                return ChannelResolution("admitted", await self._snapshot(connection, route, receiver))
            route = await _one(connection, """SELECT route_id,version FROM APPLICATION_CHANNEL_ROUTES
                WHERE receiver_id=? AND provider_identity=? AND session_key=?""",
                (receiver["receiver_id"], envelope.provider_identity, envelope.session_key))
            if ((route["route_id"] if route else None) != proof.get("prior_route_id")
                    or (route["version"] if route else None) != proof.get("prior_route_version")):
                raise EmbedError("channel_selection_invalid", 403)
            context = payload.get(str(choice_id))
            if not str(choice_id).isdigit() or not context:
                raise EmbedError("channel_selection_invalid", 403)
            result = await self._open_route(connection, receiver, envelope, link, context)
            payload["_proof"]["result_route_version"] = result.admission.route_version
            await connection.execute("""UPDATE APPLICATION_CHANNEL_CHALLENGES SET consumed_at=?,consumed_event_id=?,result_route_id=?,payload_json=?
                WHERE challenge_id=?""", (self.store.now(), envelope.event_id, result.admission.route_id, _json(payload), challenge_id))
            return result

    async def _open_route(self, connection, receiver, envelope, link, context):
        actor = SimpleNamespace(app_id=link["app_id"], subject=link["subject"], user_id=link["user_id"])
        # Resolve again instead of trusting a previously displayed choice.
        allowed = await self._contexts(connection, receiver, link)
        context = next((row for row in allowed if row["context_id"] == context["context_id"]), None)
        if context is None:
            raise EmbedError("channel_context_unavailable", 403)
        request = OpenConversationRequest(operation_id="channel-" + _digest(_json([receiver["receiver_id"], envelope.event_id, link["link_id"]])),
                                          context_ref=context["context_ref"], assistant_id=context["assistant_id"])
        opened = await self.applications._open_in_connection(connection, actor, request, entry_channel=receiver["channel"])
        prior = await _one(connection, """SELECT * FROM APPLICATION_CHANNEL_ROUTES
            WHERE receiver_id=? AND provider_identity=? AND session_key=?""", (receiver["receiver_id"], envelope.provider_identity, envelope.session_key))
        route_id = prior["route_id"] if prior else secrets.token_urlsafe(24)
        await connection.execute("""INSERT INTO APPLICATION_CHANNEL_ROUTES
            (route_id,receiver_id,link_id,provider_identity,session_key,conversation_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(receiver_id,provider_identity,session_key) DO UPDATE SET
            link_id=excluded.link_id,conversation_id=excluded.conversation_id,active=1,version=version+1,
            response_mode='text',updated_at=excluded.updated_at,last_operation_id=NULL""",
            (route_id, receiver["receiver_id"], link["link_id"], envelope.provider_identity, envelope.session_key,
             opened["conversation_id"], self.store.now(), self.store.now()))
        route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (route_id,))
        return ChannelResolution("admitted", await self._snapshot(connection, route, receiver))

    async def move_route_in_transaction(self, connection, admission, target, operation_id, *, replay=False):
        """F3 handoff CAS, inside the same transaction as its durable operation."""
        if replay:
            route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (admission.route_id,))
            if (not route or route["last_operation_id"] != operation_id or route["conversation_id"] != target.conversation_id
                    or route["version"] != admission.route_version + 1):
                raise EmbedError("channel_route_changed", 409)
            return await self._snapshot(connection, route)
        await self.revalidate(connection, admission)
        if (target.app_id, target.subject, target.user_id, target.context_id) != (
                admission.scope.app_id, admission.scope.subject, admission.scope.user_id, admission.scope.context_id):
            raise EmbedError("channel_scope_mismatch", 403)
        cursor = await connection.execute("""UPDATE APPLICATION_CHANNEL_ROUTES SET conversation_id=?,version=version+1,
            last_operation_id=?,updated_at=? WHERE route_id=? AND version=? AND conversation_id=? AND active=1""",
            (target.conversation_id, operation_id, self.store.now(), admission.route_id, admission.route_version, admission.scope.conversation_id))
        if cursor.rowcount != 1:
            raise EmbedError("channel_route_changed", 409)
        await connection.execute('UPDATE APPLICATION_CHANNEL_DESTINATIONS SET conversation_id=?,operation_id=? WHERE link_id=?',
            (target.conversation_id, operation_id, admission.link_id))
        route = await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (admission.route_id,))
        return await self._snapshot(connection, route)

    async def open_conversation(self, admission, request: OpenConversationRequest):
        request = OpenConversationRequest.model_validate(request)
        async with self.store.transaction() as connection:
            await self.revalidate(connection, admission)
            participant = await _one(connection, "SELECT context_ref FROM APPLICATION_PARTICIPANTS WHERE app_id=? AND subject=? AND context_id=?",
                (admission.scope.app_id, admission.scope.subject, admission.scope.context_id))
            if request.context_ref is not None and request.context_ref != participant["context_ref"]:
                raise EmbedError("channel_scope_mismatch", 403)
            request = request.model_copy(update={"context_ref": participant["context_ref"]})
            if request.conversation_id is not None and request.assistant_id is None:
                selected = await self.applications._authorize_actor_destination(connection, admission.scope, request.conversation_id)
                request = request.model_copy(update={"assistant_id": selected.assistant_id})
            opened = await self.applications._open_in_connection(connection, admission.scope, request)
            target = await self.applications._authorize_actor_destination(connection, admission.scope, opened["conversation_id"])
            return await self.move_route_in_transaction(connection, admission, target, request.operation_id)

    async def reset_entry(self, admission, operation_id):
        from .handoff import resolve_entry_assistant
        async with self.store.connection(readonly=True) as connection:
            await self.revalidate(connection, admission)
            receiver = await _one(connection, "SELECT channel FROM APPLICATION_CHANNEL_RECEIVERS WHERE receiver_id=?", (admission.receiver_id,))
            assistant = await resolve_entry_assistant(self.applications, connection, admission.scope, admission.scope.context_id, receiver["channel"],
                                                      required_capability=receiver["channel"])
        return await self.open_conversation(admission, OpenConversationRequest(operation_id=operation_id, assistant_id=assistant))

    async def set_response_mode(self, admission, mode):
        if mode not in {"text", "voice"}:
            raise EmbedError("invalid_request", 400)
        async with self.store.transaction() as connection:
            await self.revalidate(connection, admission, capability="tts" if mode == "voice" else None)
            await connection.execute("UPDATE APPLICATION_CHANNEL_ROUTES SET response_mode=?,version=version+1,updated_at=? WHERE route_id=? AND version=?",
                (mode, self.store.now(), admission.route_id, admission.route_version))
            return await self._snapshot(connection, await _one(connection, "SELECT * FROM APPLICATION_CHANNEL_ROUTES WHERE route_id=?", (admission.route_id,)))

    async def list_assistants(self, admission):
        from .handoff import ApplicationHandoffService
        await self.resolve_runtime(admission)
        return (await ApplicationHandoffService(self.store).state(admission.scope, admission.scope.conversation_id))["assistants"]

    async def list_conversations(self, admission):
        async with self.store.connection(readonly=True) as connection:
            await self.revalidate(connection, admission)
            rows = await (await connection.execute("""SELECT conversation_id,assistant_id FROM APPLICATION_CONVERSATIONS
                WHERE app_id=? AND subject=? AND context_id=? ORDER BY conversation_id DESC""",
                (admission.scope.app_id, admission.scope.subject, admission.scope.context_id))).fetchall()
            result = []
            for row in rows:
                try:
                    context = await self.applications._authorize_actor_destination(connection, admission.scope, row["conversation_id"])
                    await self.applications._require_unlocked(connection, context.conversation_id)
                    result.append(dict(row))
                except EmbedError:
                    continue
            return result

    async def link_state(self, app_id, external_user_id):
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.connection(readonly=True) as connection:
            rows = await (await connection.execute("""SELECT l.link_id,l.receiver_id,l.provider_identity,l.active,l.version,
                r.channel FROM APPLICATION_CHANNEL_LINKS l JOIN APPLICATION_CHANNEL_RECEIVERS r USING(receiver_id)
                WHERE l.app_id=? AND l.subject=? ORDER BY l.receiver_id""", (app_id, account["subject"]))).fetchall()
            return [dict(row) for row in rows]

    async def account_options(self, app_id, external_user_id):
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.connection(readonly=True) as connection:
            await self.store._app(connection, app_id)
            await self.store._live(connection, app_id, account["user_id"], account["subject"])
            receivers = await (await connection.execute("""SELECT receiver_id,channel,receiver_key FROM APPLICATION_CHANNEL_RECEIVERS
                WHERE app_id=? AND enabled=1 ORDER BY receiver_id""", (app_id,))).fetchall()
            return {"receivers": [dict(row) for row in receivers], "links": await self.link_state(app_id, external_user_id)}

    async def select_context_for_account(self, app_id, external_user_id, challenge_id, choice_id, operation_id):
        """An authenticated app account may answer its already-proven selector.

        Provider identity comes from the stored proof, never client parameters.
        """
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.connection(readonly=True) as connection:
            challenge = await _one(connection, """SELECT c.*,r.channel,r.provider,r.provider_account_id,r.receiver_key
                FROM APPLICATION_CHANNEL_CHALLENGES c JOIN APPLICATION_CHANNEL_RECEIVERS r USING(receiver_id)
                WHERE c.challenge_id=? AND c.app_id=? AND c.subject=? AND c.purpose='context'""",
                (challenge_id, app_id, account["subject"]))
            if not challenge:
                raise EmbedError("channel_selection_invalid", 403)
        envelope = VerifiedChannelEnvelope(challenge["channel"], challenge["provider"], challenge["receiver_key"],
            challenge["provider_identity"], challenge["session_key"], "backend-choice-" + _digest(operation_id), challenge["provider_account_id"])
        return await self.select_context(envelope, challenge_id, choice_id)

    async def unlink(self, app_id, external_user_id, link_id, *, expected_version):
        from .accounts import ApplicationAccountService
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id)
            await self.store._live(connection, app_id, account["user_id"], account["subject"])
            cursor = await connection.execute("""UPDATE APPLICATION_CHANNEL_LINKS SET active=0,version=version+1,proof_secret_hash=NULL
                WHERE app_id=? AND subject=? AND link_id=? AND version=?""", (app_id, account["subject"], link_id, expected_version))
            if cursor.rowcount != 1:
                raise EmbedError("channel_link_changed", 409)
            await connection.execute("UPDATE APPLICATION_CHANNEL_ROUTES SET active=0,version=version+1 WHERE link_id=?", (link_id,))
            await connection.execute("""DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE app_id=? AND subject=?
                AND receiver_id=(SELECT receiver_id FROM APPLICATION_CHANNEL_LINKS WHERE link_id=?)""",
                (app_id, account["subject"], link_id))
            return {"unlinked": True}

    async def reset_access_pin(self, app_id, external_user_id, link_id, pin, *, expected_version):
        from .accounts import ApplicationAccountService
        if not isinstance(pin, str) or not pin.isascii() or not pin.isdigit() or not 6 <= len(pin) <= 10:
            raise EmbedError("invalid_request", 400)
        account = await ApplicationAccountService(self.store).account_binding(app_id, external_user_id)
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id)
            await self.store._live(connection, app_id, account["user_id"], account["subject"])
            cursor = await connection.execute("""UPDATE APPLICATION_CHANNEL_LINKS SET proof_secret_hash=?,version=version+1
                WHERE app_id=? AND subject=? AND link_id=? AND version=? AND active=1""",
                (_secret_hash(pin), app_id, account["subject"], link_id, expected_version))
            if cursor.rowcount != 1:
                raise EmbedError("channel_link_changed", 409)
            await connection.execute("UPDATE APPLICATION_CHANNEL_ROUTES SET active=0,version=version+1 WHERE link_id=?", (link_id,))
            await connection.execute("""DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE app_id=? AND subject=?
                AND receiver_id=(SELECT receiver_id FROM APPLICATION_CHANNEL_LINKS WHERE link_id=?)
                AND provider_identity=(SELECT provider_identity FROM APPLICATION_CHANNEL_LINKS WHERE link_id=?)""",
                (app_id, account["subject"], link_id, link_id))
            return {"reset": True}


async def delete_user_channels_in_transaction(connection, user_id):
    exists = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_CHANNEL_LINKS'")
    if not exists:
        return
    subjects = "SELECT subject FROM EMBED_SUBJECTS WHERE user_id=?"
    pairs = await (await connection.execute(f"""SELECT receiver_id,provider_identity FROM APPLICATION_CHANNEL_LINKS WHERE subject IN ({subjects})
        UNION SELECT receiver_id,provider_identity FROM APPLICATION_CHANNEL_CHALLENGES WHERE subject IN ({subjects})""", (user_id, user_id))).fetchall()
    await connection.execute(f"DELETE FROM APPLICATION_CHANNEL_ROUTES WHERE link_id IN (SELECT link_id FROM APPLICATION_CHANNEL_LINKS WHERE subject IN ({subjects}))", (user_id,))
    await connection.execute(f"DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE subject IN ({subjects})", (user_id,))
    await connection.execute(f"DELETE FROM APPLICATION_CHANNEL_LINKS WHERE subject IN ({subjects})", (user_id,))
    for pair in pairs:
        other_link = await _one(connection, "SELECT 1 FROM APPLICATION_CHANNEL_LINKS WHERE receiver_id=? AND provider_identity=?", tuple(pair))
        other_proof = await _one(connection, "SELECT 1 FROM APPLICATION_CHANNEL_CHALLENGES WHERE receiver_id=? AND provider_identity=? AND subject IS NOT NULL", tuple(pair))
        if not other_link and not other_proof:
            await connection.execute("DELETE FROM APPLICATION_CHANNEL_CHALLENGES WHERE receiver_id=? AND provider_identity=? AND subject IS NULL", tuple(pair))
