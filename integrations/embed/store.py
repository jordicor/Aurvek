"""Durable, revocable delegated identity and interview bindings.

All bearer values are random and stored only as SHA-256 digests. BEGIN IMMEDIATE
serializes single-use exchanges and interview creation together with native chat
creation, so retries cannot create an orphan or a second interview.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import urlsplit

from .models import AppConfig, CONTRACT_VERSION, EmbedError, EmbedPrincipal, InterviewBrief
from i18n import LANGUAGES

SCHEMA = """
CREATE TABLE IF NOT EXISTS EMBED_APPS (
 app_id TEXT PRIMARY KEY, config_json TEXT NOT NULL, client_secret_hash TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS EMBED_HOSTS (
 authority TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 origin TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS EMBED_SUBJECTS (
 subject TEXT PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES USERS(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS EMBED_MEMBERSHIPS (
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 subject TEXT NOT NULL REFERENCES EMBED_SUBJECTS(subject) ON DELETE CASCADE, active INTEGER NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, capabilities_json TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(app_id,subject)
);
CREATE TABLE IF NOT EXISTS EMBED_AUTH_TRANSACTIONS (
 token_hash TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 app_version INTEGER NOT NULL, redirect_uri TEXT NOT NULL, state TEXT NOT NULL,
 code_challenge TEXT NOT NULL, ui_language TEXT NOT NULL,
 expires_at INTEGER NOT NULL, consumed_at INTEGER
);
CREATE TABLE IF NOT EXISTS EMBED_AUTH_CODES (
 code_hash TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 app_version INTEGER NOT NULL, subject TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
 session_version INTEGER NOT NULL, membership_version INTEGER NOT NULL,
 source_session_hash TEXT NOT NULL, source_expires_at INTEGER NOT NULL,
 redirect_uri TEXT NOT NULL, code_challenge TEXT NOT NULL,
 expires_at INTEGER NOT NULL, consumed_at INTEGER, source_auth_time INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS EMBED_DELEGATED_SESSIONS (
 session_id TEXT PRIMARY KEY, credential_hash TEXT NOT NULL UNIQUE,
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id), app_version INTEGER NOT NULL,
 subject TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
 session_version INTEGER NOT NULL, membership_version INTEGER NOT NULL,
 source_session_hash TEXT NOT NULL, csrf_token TEXT NOT NULL,
 expires_at INTEGER NOT NULL, revoked_at INTEGER, created_at INTEGER NOT NULL,
 source_auth_time INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_embed_delegated_member
 ON EMBED_DELEGATED_SESSIONS(app_id,subject);
CREATE TABLE IF NOT EXISTS EMBED_INTERVIEWS (
 app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id), subject TEXT NOT NULL,
 external_project_id TEXT NOT NULL, user_id INTEGER NOT NULL REFERENCES USERS(id) ON DELETE CASCADE,
 conversation_id INTEGER UNIQUE REFERENCES CONVERSATIONS(id) ON DELETE SET NULL,
 prompt_id INTEGER REFERENCES PROMPTS(id) ON DELETE SET NULL,
 brief_json TEXT NOT NULL, brief_revision INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'active', created_at INTEGER NOT NULL,
 updated_at INTEGER NOT NULL, PRIMARY KEY(app_id,subject,external_project_id)
);
CREATE TABLE IF NOT EXISTS EMBED_IDEMPOTENCY (
 app_id TEXT NOT NULL, subject TEXT NOT NULL REFERENCES EMBED_SUBJECTS(subject) ON DELETE CASCADE, idempotency_key TEXT NOT NULL,
 external_project_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 PRIMARY KEY(app_id,subject,idempotency_key)
);
CREATE TABLE IF NOT EXISTS EMBED_BRIEF_REVISIONS (
 conversation_id INTEGER NOT NULL REFERENCES CONVERSATIONS(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL, brief_json TEXT NOT NULL, applied_at INTEGER NOT NULL,
 PRIMARY KEY(conversation_id,revision)
);
CREATE TABLE IF NOT EXISTS EMBED_TICKETS (
 ticket_hash TEXT PRIMARY KEY, delegated_session_id TEXT NOT NULL REFERENCES EMBED_DELEGATED_SESSIONS(session_id) ON DELETE CASCADE,
 app_id TEXT NOT NULL, subject TEXT NOT NULL, external_project_id TEXT NOT NULL,
 conversation_id INTEGER NOT NULL REFERENCES CONVERSATIONS(id) ON DELETE CASCADE, parent_origin TEXT NOT NULL,
 embed_origin TEXT NOT NULL, ui_language TEXT NOT NULL, frame_instance_id TEXT NOT NULL,
 expires_at INTEGER NOT NULL, consumed_at INTEGER,
 binding_kind TEXT NOT NULL DEFAULT 'embed', context_id TEXT
);
CREATE TABLE IF NOT EXISTS EMBED_BROWSER_SESSIONS (
 cookie_hash TEXT PRIMARY KEY, delegated_session_id TEXT NOT NULL REFERENCES EMBED_DELEGATED_SESSIONS(session_id) ON DELETE CASCADE,
 embed_origin TEXT NOT NULL, expires_at INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS EMBED_FRAMES (
 frame_instance_id TEXT NOT NULL, delegated_session_id TEXT NOT NULL REFERENCES EMBED_DELEGATED_SESSIONS(session_id) ON DELETE CASCADE,
 app_id TEXT NOT NULL, subject TEXT NOT NULL, external_project_id TEXT NOT NULL,
 conversation_id INTEGER NOT NULL REFERENCES CONVERSATIONS(id) ON DELETE CASCADE, parent_origin TEXT NOT NULL,
 embed_origin TEXT NOT NULL, ui_language TEXT NOT NULL, expires_at INTEGER NOT NULL,
 binding_kind TEXT NOT NULL DEFAULT 'embed', context_id TEXT,
 PRIMARY KEY(app_id,subject,frame_instance_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_embed_frame_identity ON EMBED_FRAMES(app_id,frame_instance_id);
CREATE TABLE IF NOT EXISTS EMBED_REGISTRATIONS (
 verification_hash TEXT PRIMARY KEY, app_id TEXT NOT NULL REFERENCES EMBED_APPS(app_id),
 prompt_id INTEGER NOT NULL, issuer TEXT NOT NULL, expires_at INTEGER NOT NULL,
 created_at INTEGER NOT NULL
);
"""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


async def _one(connection, sql: str, values=()):
    async with connection.execute(sql, values) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


class EmbedStore:
    def __init__(self, connection_factory=None, *, clock=None, user_loader=None,
                 permission_checker=None, revoked_checker=None, conversation_creator=None):
        self._factory = connection_factory
        self._clock = clock or time.time
        self._user_loader = user_loader
        self._permission_checker = permission_checker
        self._revoked_checker = revoked_checker
        self._conversation_creator = conversation_creator

    def connection(self, readonly=False):
        if self._factory:
            return self._factory(readonly=readonly)
        from database import get_db_connection
        return get_db_connection(readonly=readonly)

    @asynccontextmanager
    async def transaction(self):
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    def now(self) -> int:
        return int(self._clock())

    async def initialize(self):
        async with self.connection() as connection:
            await connection.executescript(SCHEMA)
            await connection.commit()
        from integrations.applications.service import ApplicationService
        await ApplicationService(self).initialize()

    async def register_app(self, config: AppConfig, client_secret: str | None = None) -> None:
        """Operator-only; preserve an existing key unless explicitly rotating it."""
        if client_secret is not None and (len(client_secret) < 32 or len(client_secret) > 256
                or not client_secret.isascii() or any(ord(c) < 33 for c in client_secret)):
            raise ValueError("Use a random client secret of at least 32 characters")
        secret_hash = _digest(client_secret) if client_secret is not None else None
        from marketplace.middleware.custom_domains import is_primary_domain
        primary_hosts = {urlsplit("https://" + os.getenv(name, "").lower().strip()).hostname
                         for name in ("PRIMARY_APP_DOMAIN", "CLOUDFLARE_DOMAIN")}
        primary_hosts.add(urlsplit(config.issuer).hostname)
        if any(urlsplit(origin).hostname in primary_hosts
               or is_primary_domain(urlsplit(origin).hostname)
               or urlsplit(origin).hostname == "::1"
               for origin in config.embed_origins):
            raise ValueError("Embed hosts must be separate from native identity hosts")
        async with self.transaction() as connection:
            old = await _one(connection, "SELECT * FROM EMBED_APPS WHERE app_id=?", (config.app_id,))
            if secret_hash is None:
                if not old:
                    raise ValueError("A new application requires a client secret")
                secret_hash = old["client_secret_hash"]
            if old:
                old_config = AppConfig.model_validate_json(old["config_json"])
                if config.issuer != old_config.issuer or config.prompt_id != old_config.prompt_id:
                    raise ValueError("Registered issuer and prompt are immutable; use another app ID")
            await connection.execute(
                """INSERT INTO EMBED_APPS
                (app_id,config_json,client_secret_hash,enabled,created_at,updated_at)
                VALUES (?,?,?,?,?,?) ON CONFLICT(app_id) DO UPDATE SET
                config_json=excluded.config_json,client_secret_hash=excluded.client_secret_hash,
                enabled=excluded.enabled,version=version+1,updated_at=excluded.updated_at""",
                (config.app_id, config.model_dump_json(), secret_hash, int(config.enabled), self.now(), self.now()),
            )
            await connection.execute("DELETE FROM EMBED_HOSTS WHERE app_id=?", (config.app_id,))
            for origin in config.embed_origins:
                await connection.execute("INSERT INTO EMBED_HOSTS VALUES (?,?,?)",
                                         (urlsplit(origin).netloc, config.app_id, origin))
            from integrations.applications.service import ApplicationService
            await ApplicationService(self).seed_app(connection, config)

    async def _app(self, connection, app_id: str, *, active=True):
        row = await _one(connection, "SELECT * FROM EMBED_APPS WHERE app_id=?", (app_id,))
        if not row or (active and not row["enabled"]):
            raise EmbedError("unauthenticated")
        return row, AppConfig.model_validate_json(row["config_json"])

    async def get_app(self, app_id: str) -> AppConfig:
        async with self.connection(readonly=True) as connection:
            _, config = await self._app(connection, app_id)
            return config

    async def set_app_enabled(self, app_id: str, enabled: bool) -> None:
        async with self.transaction() as connection:
            _, config = await self._app(connection, app_id, active=False)
            config.enabled = enabled
            await connection.execute("""UPDATE EMBED_APPS SET config_json=?,enabled=?,version=version+1,
                updated_at=? WHERE app_id=?""", (config.model_dump_json(), int(enabled), self.now(), app_id))

    async def app_for_host(self, authority: str) -> AppConfig | None:
        if not isinstance(authority, str) or any(c.isspace() for c in authority):
            return None
        async with self.connection(readonly=True) as connection:
            try:
                row = await _one(connection, """SELECT a.config_json FROM EMBED_HOSTS h
                    JOIN EMBED_APPS a USING(app_id) WHERE h.authority=?""", (authority.lower(),))
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return None  # Native startup before the additive migration.
                raise
        return AppConfig.model_validate_json(row["config_json"]) if row else None

    async def authenticate_client(self, app_id: str, client_secret: str) -> AppConfig:
        async with self.connection(readonly=True) as connection:
            row, config = await self._app(connection, app_id)
            try:
                valid = hmac.compare_digest(row["client_secret_hash"], _digest(client_secret))
            except (UnicodeError, AttributeError):
                valid = False
            if not valid:
                raise EmbedError("unauthenticated")
            return config

    async def provision_membership(self, app_id: str, user_id: int,
                                   capabilities: dict[str, bool] | None = None, *, active: bool = True) -> str:
        """Explicit operator invitation; no email matching or global preference writes."""
        async with self.transaction() as connection:
            await self._app(connection, app_id, active=False)
            user = await _one(connection, "SELECT id FROM USERS WHERE id=?", (user_id,))
            if not user:
                raise ValueError("User does not exist")
            row = await _one(connection, "SELECT subject FROM EMBED_SUBJECTS WHERE user_id=?", (user_id,))
            subject = row["subject"] if row else secrets.token_urlsafe(24)
            if not row:
                await connection.execute("INSERT INTO EMBED_SUBJECTS VALUES (?,?)", (subject, user_id))
            await connection.execute("""INSERT INTO EMBED_MEMBERSHIPS
                VALUES (?,?,?,1,?,?,?) ON CONFLICT(app_id,subject) DO UPDATE SET
                active=excluded.active,version=version+1,capabilities_json=excluded.capabilities_json,
                updated_at=excluded.updated_at""",
                (app_id, subject, int(active), _json(capabilities or {"text": True}), self.now(), self.now()))
            return subject

    async def revoke_app_membership(self, app_id: str, subject: str) -> list[int]:
        async with self.transaction() as connection:
            await connection.execute("""UPDATE EMBED_MEMBERSHIPS SET active=0,version=version+1,
                updated_at=? WHERE app_id=? AND subject=? AND active=1""", (self.now(), app_id, subject))
            await connection.execute("""UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=?
                WHERE app_id=? AND subject=? AND revoked_at IS NULL""", (self.now(), app_id, subject))
            async with connection.execute("""SELECT conversation_id FROM EMBED_INTERVIEWS
                WHERE app_id=? AND subject=? AND conversation_id IS NOT NULL
                UNION SELECT conversation_id FROM APPLICATION_CONVERSATIONS WHERE app_id=? AND subject=?""",
                                          (app_id, subject, app_id, subject)) as cursor:
                return [int(row[0]) for row in await cursor.fetchall()]

    async def authorize_begin(self, app_id: str, redirect_uri: str, state: str,
                              code_challenge: str, ui_language: str = "en") -> str:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge)
                or not 16 <= len(state) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in state)
                or ui_language not in LANGUAGES):
            raise EmbedError("invalid_request", 400)
        token = secrets.token_urlsafe(32)
        async with self.transaction() as connection:
            row, config = await self._app(connection, app_id)
            if redirect_uri not in config.redirect_uris:
                raise EmbedError("invalid_request", 400)
            await connection.execute("INSERT INTO EMBED_AUTH_TRANSACTIONS VALUES (?,?,?,?,?,?,?,?,NULL)",
                (_digest(token), app_id, row["version"], redirect_uri, state, code_challenge, ui_language, self.now() + 600))
        return token

    async def _live(self, connection, app_id: str, user_id: int, subject: str):
        user = await _one(connection, "SELECT id,is_enabled,session_version FROM USERS WHERE id=?", (user_id,))
        if not user or not user["is_enabled"]:
            raise EmbedError("unauthenticated")
        member = await _one(connection, """SELECT m.* FROM EMBED_MEMBERSHIPS m
            JOIN EMBED_SUBJECTS s USING(subject) WHERE m.app_id=? AND m.subject=? AND s.user_id=?""",
            (app_id, subject, user_id))
        if not member or not member["active"]:
            raise EmbedError("membership_inactive", 403)
        if self._revoked_checker:
            revoked = await self._revoked_checker(user_id)
        else:
            from rediscfg import is_user_revoked
            revoked = await is_user_revoked(user_id)
        if revoked:
            raise EmbedError("unauthenticated")
        return user, member

    async def authorization_app(self, transaction_token: str) -> AppConfig:
        async with self.connection(readonly=True) as connection:
            row = await _one(connection, "SELECT * FROM EMBED_AUTH_TRANSACTIONS WHERE token_hash=?",
                             (_digest(transaction_token),))
            if not row or row["consumed_at"] is not None or row["expires_at"] <= self.now():
                raise EmbedError("unauthenticated")
            app, config = await self._app(connection, row["app_id"])
            if app["version"] != row["app_version"]:
                raise EmbedError("unauthenticated")
            return config

    async def authorize_complete(self, transaction_token: str, user, source_session: str) -> dict:
        code = secrets.token_urlsafe(32)
        async with self.transaction() as connection:
            tx = await _one(connection, "SELECT * FROM EMBED_AUTH_TRANSACTIONS WHERE token_hash=?", (_digest(transaction_token),))
            if not tx or tx["consumed_at"] is not None or tx["expires_at"] <= self.now():
                raise EmbedError("unauthenticated")
            app_row, _ = await self._app(connection, tx["app_id"])
            subject_row = await _one(connection, "SELECT subject FROM EMBED_SUBJECTS WHERE user_id=?", (user.id,))
            if not subject_row:
                raise EmbedError("membership_inactive", 403)
            live, member = await self._live(connection, tx["app_id"], user.id, subject_row["subject"])
            source_expiry = int(getattr(user, "session_expires_at", 0))
            if (app_row["version"] != tx["app_version"] or live["session_version"] != user.session_version
                    or source_expiry <= self.now() or not source_session):
                raise EmbedError("unauthenticated")
            await connection.execute("UPDATE EMBED_AUTH_TRANSACTIONS SET consumed_at=? WHERE token_hash=?",
                                     (self.now(), _digest(transaction_token)))
            await connection.execute("""INSERT INTO EMBED_AUTH_CODES
                (code_hash,app_id,app_version,subject,user_id,session_version,membership_version,
                 source_session_hash,source_expires_at,redirect_uri,code_challenge,expires_at,consumed_at,source_auth_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
                (_digest(code), tx["app_id"], tx["app_version"], subject_row["subject"], user.id,
                 live["session_version"], member["version"], _digest(source_session), source_expiry,
                 tx["redirect_uri"], tx["code_challenge"], self.now() + 90,
                 int(getattr(user, "auth_time", 0) or 0)))
            return {"code": code, "state": tx["state"], "redirect_uri": tx["redirect_uri"]}

    async def exchange_code(self, app_id: str, code: str, code_verifier: str, redirect_uri: str,
                            *, application: bool = False) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", code_verifier):
            raise EmbedError("unauthenticated")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        credential = secrets.token_urlsafe(48)
        async with self.transaction() as connection:
            app_row, config = await self._app(connection, app_id)
            row = await _one(connection, "SELECT * FROM EMBED_AUTH_CODES WHERE code_hash=? AND app_id=?", (_digest(code), app_id))
            if (not row or row["consumed_at"] is not None or row["expires_at"] <= self.now()
                    or row["source_expires_at"] <= self.now() or row["redirect_uri"] != redirect_uri
                    or row["app_version"] != app_row["version"] or redirect_uri not in config.redirect_uris
                    or not hmac.compare_digest(row["code_challenge"], challenge)):
                raise EmbedError("unauthenticated")
            user, member = await self._live(connection, app_id, row["user_id"], row["subject"])
            if user["session_version"] != row["session_version"] or member["version"] != row["membership_version"]:
                raise EmbedError("unauthenticated")
            expiry = min(self.now() + 8 * 3600, row["source_expires_at"])
            await connection.execute("UPDATE EMBED_AUTH_CODES SET consumed_at=? WHERE code_hash=?", (self.now(), _digest(code)))
            await connection.execute("""INSERT INTO EMBED_DELEGATED_SESSIONS
                (session_id,credential_hash,app_id,app_version,subject,user_id,session_version,
                 membership_version,source_session_hash,csrf_token,expires_at,revoked_at,created_at,source_auth_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (secrets.token_urlsafe(24), _digest(credential), app_id, app_row["version"], row["subject"], row["user_id"],
                 row["session_version"], row["membership_version"], row["source_session_hash"], secrets.token_urlsafe(32),
                 expiry, self.now(), row.get("source_auth_time", 0)))
        principal = await (self.inspect_identity(app_id, credential) if application
                           else self.inspect_access(app_id, credential))
        return {**self.access_response(principal), "delegated_credential": credential}

    async def _load_user(self, user_id):
        if self._user_loader:
            return await self._user_loader(user_id)
        from auth import get_user_by_id
        return await get_user_by_id(user_id)

    async def _principal(self, connection, session: dict, *, check_prompt=True) -> EmbedPrincipal:
        app_row, config = await self._app(connection, session["app_id"])
        if (session["revoked_at"] is not None or session["expires_at"] <= self.now()
                or session["app_version"] != app_row["version"]):
            raise EmbedError("unauthenticated")
        user, member = await self._live(connection, session["app_id"], session["user_id"], session["subject"])
        if user["session_version"] != session["session_version"] or member["version"] != session["membership_version"]:
            raise EmbedError("unauthenticated")
        if check_prompt:
            live_user = await self._load_user(session["user_id"])
            if self._permission_checker:
                permitted = await self._permission_checker(live_user, config.prompt_id, connection)
            else:
                from prompts import can_user_access_prompt
                async with connection.cursor() as cursor:
                    permitted = await can_user_access_prompt(live_user, config.prompt_id, cursor)
            if not live_user or not permitted:
                raise EmbedError("membership_inactive", 403)
        requested = json.loads(member["capabilities_json"])
        # Only proven pilot paths are enabled. These cannot be enabled by config alone.
        capabilities = {"text": bool(config.capabilities.get("text") and requested.get("text")),
                        "voice": False, "attachments": False, "memory": False,
                        "image_generation": False, "multi_ai": False}
        return EmbedPrincipal(app_id=config.app_id, issuer=config.issuer, subject=session["subject"],
            user_id=session["user_id"], prompt_id=config.prompt_id, delegated_session_id=session["session_id"],
            session_version=session["session_version"], membership_version=session["membership_version"],
            expires_at=session["expires_at"], capabilities=capabilities, csrf_token=session["csrf_token"],
            source_auth_time=int(session.get("source_auth_time", 0)))

    async def _session(self, connection, session_id: str) -> EmbedPrincipal:
        row = await _one(connection, "SELECT * FROM EMBED_DELEGATED_SESSIONS WHERE session_id=?", (session_id,))
        if not row:
            raise EmbedError("unauthenticated")
        return await self._principal(connection, row)

    async def _session_identity(self, connection, session_id: str) -> EmbedPrincipal:
        """Authenticate the member; the destination service checks its own prompt."""
        row = await _one(connection, "SELECT * FROM EMBED_DELEGATED_SESSIONS WHERE session_id=?", (session_id,))
        if not row:
            raise EmbedError("unauthenticated")
        return await self._principal(connection, row, check_prompt=False)

    async def inspect_identity(self, app_id: str, credential: str) -> EmbedPrincipal:
        async with self.connection(readonly=True) as connection:
            row = await _one(connection, "SELECT * FROM EMBED_DELEGATED_SESSIONS WHERE credential_hash=? AND app_id=?",
                             (_digest(credential), app_id))
            if not row:
                raise EmbedError("unauthenticated")
            return await self._principal(connection, row, check_prompt=False)

    async def inspect_access(self, app_id: str, credential: str) -> EmbedPrincipal:
        async with self.connection(readonly=True) as connection:
            row = await _one(connection, "SELECT * FROM EMBED_DELEGATED_SESSIONS WHERE credential_hash=? AND app_id=?",
                             (_digest(credential), app_id))
            if not row:
                raise EmbedError("unauthenticated")
            return await self._principal(connection, row)

    @staticmethod
    def access_response(principal: EmbedPrincipal) -> dict:
        return {"contract_version": CONTRACT_VERSION, "issuer": principal.issuer,
                "subject": principal.subject, "app_id": principal.app_id,
                "status": "active", "expires_at": principal.expires_at,
                "capabilities": principal.capabilities,
                "capability_reasons": {"voice": "pilot_validation_pending", "attachments": "private_media_validation_pending",
                                       "memory": "cross_conversation_memory_disabled"}}

    async def _binding(self, connection, principal, *, project=None, conversation_id=None):
        params = [principal.app_id, principal.subject, principal.user_id, principal.prompt_id]
        query = """SELECT i.* FROM EMBED_INTERVIEWS i JOIN CONVERSATIONS c ON c.id=i.conversation_id
            WHERE i.app_id=? AND i.subject=? AND i.user_id=? AND i.prompt_id=?
            AND c.user_id=i.user_id AND c.role_id=i.prompt_id AND i.state='active'"""
        if project is not None:
            query += " AND i.external_project_id=?"
            params.append(project)
        if conversation_id is not None:
            query += " AND i.conversation_id=?"
            params.append(conversation_id)
        row = await _one(connection, query, params)
        if not row:
            raise EmbedError("not_found", 404)
        # Adopted v1 chats retain their selection/brief contract, while context
        # revocation and assistant policy apply on every entry surface.
        adopted = await _one(connection, "SELECT 1 FROM APPLICATION_CONVERSATIONS WHERE conversation_id=?",
                             (row["conversation_id"],))
        if adopted:
            from integrations.applications.service import ApplicationService
            row["_application_context"] = await ApplicationService(self).authorize_runtime_conversation(
                connection, principal.user_id, row["conversation_id"])
        return row

    async def authorize_conversation(self, principal, conversation_id, external_project_id=None) -> dict:
        async with self.connection(readonly=True) as connection:
            if principal.binding_kind == "application":
                live = await self._session_identity(connection, principal.delegated_session_id)
                from integrations.applications.service import ApplicationService
                service = ApplicationService(self)
                context = await service.authorize_conversation(connection, live, conversation_id)
                if principal.application is None or context.context_id != principal.application.context_id:
                    raise EmbedError("not_found", 404)
                if external_project_id is not None and external_project_id != context.context_id:
                    raise EmbedError("not_found", 404)
                return await service.find_binding(connection, conversation_id)
            live = await self._session(connection, principal.delegated_session_id)
            return await self._binding(connection, live, project=external_project_id, conversation_id=conversation_id)

    @staticmethod
    def _interview_response(row, principal):
        return {"contract_version": CONTRACT_VERSION, "issuer": principal.issuer,
                "subject": principal.subject, "app_id": principal.app_id,
                "external_project_id": row["external_project_id"], "conversation_id": str(row["conversation_id"]),
                "brief_revision": row["brief_revision"], "state": row["state"],
                "created_at": row["created_at"], "updated_at": row["updated_at"],
                "capabilities": principal.capabilities}

    async def ensure_interview(self, principal, external_project_id: str,
                               idempotency_key: str, brief: InterviewBrief) -> dict:
        brief_json = _json(brief.model_dump())
        request_hash = _digest(_json({"project": external_project_id, "brief": brief.model_dump()}))
        async with self.transaction() as connection:
            principal = await self._session(connection, principal.delegated_session_id)
            if not principal.capabilities.get("text"):
                raise EmbedError("membership_inactive", 403)
            existing_key = await _one(connection, "SELECT * FROM EMBED_IDEMPOTENCY WHERE app_id=? AND subject=? AND idempotency_key=?",
                                      (principal.app_id, principal.subject, idempotency_key))
            if existing_key and existing_key["request_hash"] != request_hash:
                raise EmbedError("revision_conflict", 409)
            row = await _one(connection, "SELECT * FROM EMBED_INTERVIEWS WHERE app_id=? AND subject=? AND external_project_id=?",
                             (principal.app_id, principal.subject, external_project_id))
            if row:
                row = await self._binding(connection, principal, project=external_project_id)
                applied = await _one(connection, "SELECT brief_json FROM EMBED_BRIEF_REVISIONS WHERE conversation_id=? AND revision=?",
                                     (row["conversation_id"], brief.revision))
                if applied and applied["brief_json"] != brief_json:
                    raise EmbedError("revision_conflict", 409)
            else:
                user = await self._load_user(principal.user_id)
                creator = self._conversation_creator
                if creator is None:
                    from chat.services.conversations import create_conversation_core
                    creator = create_conversation_core
                try:
                    async with connection.cursor() as cursor:
                        conversation_id = await creator(principal.user_id, cursor, user,
                            prompt_id=principal.prompt_id, strict_prompt_access=True)
                except PermissionError as error:
                    raise EmbedError("membership_inactive", 403) from error
                except ValueError as error:
                    raise EmbedError("service_unavailable", 503) from error
                await connection.execute("INSERT INTO EMBED_INTERVIEWS VALUES (?,?,?,?,?,?,?,?,'active',?,?)",
                    (principal.app_id, principal.subject, external_project_id, principal.user_id, conversation_id,
                     principal.prompt_id, brief_json, brief.revision, self.now(), self.now()))
                await connection.execute("INSERT INTO EMBED_BRIEF_REVISIONS VALUES (?,?,?,?)",
                                         (conversation_id, brief.revision, brief_json, self.now()))
                row = await self._binding(connection, principal, project=external_project_id)
            if not existing_key:
                await connection.execute("INSERT INTO EMBED_IDEMPOTENCY VALUES (?,?,?,?,?)",
                    (principal.app_id, principal.subject, idempotency_key, external_project_id, request_hash))
            from integrations.applications.service import ApplicationService
            await ApplicationService(self).adopt_legacy(connection, row)
            return self._interview_response(row, principal)

    async def interview_state(self, principal, external_project_id, conversation_id=None):
        async with self.connection(readonly=True) as connection:
            principal = await self._session(connection, principal.delegated_session_id)
            row = await self._binding(connection, principal, project=external_project_id, conversation_id=conversation_id)
            return self._interview_response(row, principal)

    async def update_brief(self, principal, external_project_id, conversation_id,
                           expected_revision: int, brief: InterviewBrief):
        payload = _json(brief.model_dump())
        async with self.transaction() as connection:
            principal = await self._session(connection, principal.delegated_session_id)
            row = await self._binding(connection, principal, project=external_project_id, conversation_id=conversation_id)
            applied = await _one(connection, "SELECT brief_json FROM EMBED_BRIEF_REVISIONS WHERE conversation_id=? AND revision=?",
                                 (row["conversation_id"], brief.revision))
            if applied:
                if payload != applied["brief_json"]:
                    raise EmbedError("revision_conflict", 409)
                return self._interview_response(row, principal)
            if expected_revision != row["brief_revision"] or brief.revision != expected_revision + 1:
                raise EmbedError("revision_conflict", 409)
            await connection.execute("""UPDATE EMBED_INTERVIEWS SET brief_json=?,brief_revision=?,updated_at=?
                WHERE app_id=? AND subject=? AND external_project_id=?""",
                (payload, brief.revision, self.now(), principal.app_id, principal.subject, external_project_id))
            await connection.execute("INSERT INTO EMBED_BRIEF_REVISIONS VALUES (?,?,?,?)",
                                     (row["conversation_id"], brief.revision, payload, self.now()))
            row = await self._binding(connection, principal, project=external_project_id)
            return self._interview_response(row, principal)

    async def get_conversation_context(self, conversation_id: int) -> dict | None:
        async with self.connection(readonly=True) as connection:
            try:
                row = await _one(connection, "SELECT * FROM EMBED_INTERVIEWS WHERE conversation_id=?", (conversation_id,))
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return None
                raise
            if row:
                row["brief"] = json.loads(row.pop("brief_json"))
            return row

    async def issue_bootstrap(self, principal, external_project_id, conversation_id,
                              parent_origin, embed_origin, ui_language, frame_instance_id,
                              *, binding_kind="embed"):
        ticket = secrets.token_urlsafe(32)
        async with self.transaction() as connection:
            if binding_kind not in {"embed", "application"}:
                raise EmbedError("invalid_request", 400)
            context_id = None
            if binding_kind == "application":
                principal = await self._session_identity(connection, principal.delegated_session_id)
                from integrations.applications.service import ApplicationService
                context = await ApplicationService(self).authorize_conversation(connection, principal, conversation_id)
                context_id = context.context_id
                external_project_id = context_id
                row = {"conversation_id": context.conversation_id}
            else:
                principal = await self._session(connection, principal.delegated_session_id)
                row = await self._binding(connection, principal, project=external_project_id, conversation_id=conversation_id)
            _, config = await self._app(connection, principal.app_id)
            if parent_origin not in config.parent_origins or embed_origin not in config.embed_origins:
                raise EmbedError("invalid_request", 400)
            if ui_language not in LANGUAGES or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", frame_instance_id):
                raise EmbedError("invalid_request", 400)
            existing_frame = await _one(connection, "SELECT 1 FROM EMBED_FRAMES WHERE app_id=? AND frame_instance_id=?",
                                        (principal.app_id, frame_instance_id))
            if existing_frame:
                raise EmbedError("revision_conflict", 409)
            expiry = min(self.now() + 60, principal.expires_at)
            await connection.execute("""INSERT INTO EMBED_TICKETS
                (ticket_hash,delegated_session_id,app_id,subject,external_project_id,
                 conversation_id,parent_origin,embed_origin,ui_language,frame_instance_id,
                 expires_at,consumed_at,binding_kind,context_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)""",
                (_digest(ticket), principal.delegated_session_id, principal.app_id, principal.subject, external_project_id,
                 row["conversation_id"], parent_origin, embed_origin, ui_language, frame_instance_id, expiry,
                 binding_kind, context_id))
            return {"contract_version": CONTRACT_VERSION, "embed_url": embed_origin + "/embed/bootstrap",
                    "ticket": ticket, "expires_at": expiry, "frame_instance_id": frame_instance_id}

    @staticmethod
    def _with_frame(principal, frame):
        return replace(principal, conversation_id=frame["conversation_id"], external_project_id=frame["external_project_id"],
            parent_origin=frame["parent_origin"], embed_origin=frame["embed_origin"],
            frame_instance_id=frame["frame_instance_id"], ui_language=frame["ui_language"],
            binding_kind=frame.get("binding_kind", "embed"))

    async def _frame_destination(self, connection, principal, frame):
        """Transport identity and destination permission are distinct checks."""
        if frame.get("binding_kind", "embed") == "application":
            from integrations.applications.service import ApplicationService
            context = await ApplicationService(self).authorize_conversation(
                connection, principal, frame["conversation_id"])
            if context.context_id != frame.get("context_id"):
                raise EmbedError("not_found", 404)
            principal = replace(principal, prompt_id=context.prompt_id,
                                capabilities=dict(context.capabilities), application=context)
        else:
            principal = await self._session(connection, principal.delegated_session_id)
            binding = await self._binding(connection, principal, project=frame["external_project_id"],
                                          conversation_id=frame["conversation_id"])
            context = binding.get("_application_context")
            if context is not None:
                principal = replace(principal, application=context, capabilities=dict(context.capabilities))
        return self._with_frame(principal, frame)

    async def consume_bootstrap(self, ticket: str, host: str, parent_origin: str):
        cookie = secrets.token_urlsafe(48)
        async with self.transaction() as connection:
            row = await _one(connection, "SELECT * FROM EMBED_TICKETS WHERE ticket_hash=?", (_digest(ticket),))
            if (not row or row["expires_at"] <= self.now() or row["consumed_at"] is not None
                    or row["parent_origin"] != parent_origin or urlsplit(row["embed_origin"]).netloc != host.lower()):
                raise EmbedError("unauthenticated")
            principal = await self._session_identity(connection, row["delegated_session_id"])
            principal = await self._frame_destination(connection, principal, row)
            await connection.execute("UPDATE EMBED_TICKETS SET consumed_at=? WHERE ticket_hash=?", (self.now(), _digest(ticket)))
            await connection.execute("INSERT INTO EMBED_BROWSER_SESSIONS VALUES (?,?,?,?,?)",
                (_digest(cookie), principal.delegated_session_id, row["embed_origin"], principal.expires_at, self.now()))
            try:
                await connection.execute("""INSERT INTO EMBED_FRAMES
                    (frame_instance_id,delegated_session_id,app_id,subject,external_project_id,
                     conversation_id,parent_origin,embed_origin,ui_language,expires_at,binding_kind,context_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (row["frame_instance_id"], principal.delegated_session_id, principal.app_id, principal.subject,
                     row["external_project_id"], row["conversation_id"], row["parent_origin"], row["embed_origin"],
                     row["ui_language"], principal.expires_at, row.get("binding_kind", "embed"), row.get("context_id")))
            except sqlite3.IntegrityError as error:
                raise EmbedError("revision_conflict", 409) from error
            return cookie, principal

    async def _browser(self, connection, cookie, host):
        row = await _one(connection, "SELECT * FROM EMBED_BROWSER_SESSIONS WHERE cookie_hash=?", (_digest(cookie),))
        if not row or row["expires_at"] <= self.now() or urlsplit(row["embed_origin"]).netloc != host.lower():
            raise EmbedError("unauthenticated")
        principal = await self._session_identity(connection, row["delegated_session_id"])
        return replace(principal, embed_origin=row["embed_origin"])

    async def authenticate_browser(self, cookie: str, host: str) -> EmbedPrincipal:
        async with self.connection(readonly=True) as connection:
            return await self._browser(connection, cookie, host)

    async def resolve_frame(self, cookie: str, host: str, app_id: str, frame_instance_id: str):
        async with self.connection(readonly=True) as connection:
            principal = await self._browser(connection, cookie, host)
            if principal.app_id != app_id:
                raise EmbedError("not_found", 404)
            row = await _one(connection, """SELECT * FROM EMBED_FRAMES WHERE app_id=? AND subject=?
                AND frame_instance_id=? AND delegated_session_id=? AND embed_origin=? AND expires_at>?""",
                (app_id, principal.subject, frame_instance_id, principal.delegated_session_id, principal.embed_origin, self.now()))
            if not row:
                raise EmbedError("not_found", 404)
            return await self._frame_destination(connection, principal, row)

    async def end_app_session(self, app_id: str, credential: str) -> list[int]:
        """Idempotently invalidate all frames; caller reconciles active chat work."""
        async with self.transaction() as connection:
            row = await _one(connection, "SELECT * FROM EMBED_DELEGATED_SESSIONS WHERE app_id=? AND credential_hash=?",
                             (app_id, _digest(credential)))
            if not row:
                return []
            await connection.execute("UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=COALESCE(revoked_at,?) WHERE session_id=?",
                                     (self.now(), row["session_id"]))
            async with connection.execute("SELECT DISTINCT conversation_id FROM EMBED_FRAMES WHERE delegated_session_id=?",
                                          (row["session_id"],)) as cursor:
                return [int(item[0]) for item in await cursor.fetchall()]

    async def revalidate_principal(self, principal: EmbedPrincipal) -> EmbedPrincipal:
        async with self.connection(readonly=True) as connection:
            if principal.application is not None and principal.frame_instance_id:
                identity = await self._session_identity(connection, principal.delegated_session_id)
                frame = await _one(connection, """SELECT * FROM EMBED_FRAMES WHERE app_id=? AND subject=?
                    AND frame_instance_id=? AND delegated_session_id=? AND expires_at>?""",
                    (identity.app_id, identity.subject, principal.frame_instance_id,
                     identity.delegated_session_id, self.now()))
                if not frame or frame["conversation_id"] != principal.conversation_id:
                    raise EmbedError("not_found", 404)
                refreshed = await self._frame_destination(connection, identity, frame)
                if principal.ui_language in LANGUAGES:
                    refreshed = replace(refreshed, ui_language=principal.ui_language)
                return refreshed
            return await self._session(connection, principal.delegated_session_id)

    async def session_for_logout(self, app_id: str, credential: str) -> dict | None:
        """Resolve even an expired/revoked credential only for idempotent closure."""
        async with self.connection(readonly=True) as connection:
            return await _one(connection, """SELECT session_id,user_id FROM EMBED_DELEGATED_SESSIONS
                WHERE app_id=? AND credential_hash=?""", (app_id, _digest(credential)))

    async def revoke_source_session(self, raw_native_token: str) -> list[str]:
        async with self.transaction() as connection:
            digest = _digest(raw_native_token)
            try:
                async with connection.execute("SELECT session_id,user_id FROM EMBED_DELEGATED_SESSIONS WHERE source_session_hash=?",
                                              (digest,)) as cursor:
                    sessions = await cursor.fetchall()
            except sqlite3.OperationalError as error:
                if "no such table" in str(error):
                    return []
                raise
            await connection.execute("UPDATE EMBED_DELEGATED_SESSIONS SET revoked_at=COALESCE(revoked_at,?) WHERE source_session_hash=?",
                                     (self.now(), digest))
            await connection.execute("UPDATE EMBED_AUTH_CODES SET consumed_at=COALESCE(consumed_at,?) WHERE source_session_hash=?",
                                     (self.now(), digest))
        from .activity import end_session_activity
        for session in sessions:
            await end_session_activity(session["session_id"], session["user_id"])
        return [session["session_id"] for session in sessions]
