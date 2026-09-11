"""Account-only frames using the existing delegated/browser session lifecycle."""

from dataclasses import replace
import re
import secrets
from urllib.parse import urlsplit

from i18n import LANGUAGES
from integrations.embed.models import EmbedError
from integrations.embed.store import _digest, _one

SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_PROFILE_FRAMES (
 app_id TEXT NOT NULL,subject TEXT NOT NULL,frame_instance_id TEXT NOT NULL,
 delegated_session_id TEXT NOT NULL REFERENCES EMBED_DELEGATED_SESSIONS(session_id) ON DELETE CASCADE,
 ticket_hash TEXT NOT NULL UNIQUE,parent_origin TEXT NOT NULL,embed_origin TEXT NOT NULL,
 ui_language TEXT NOT NULL,ticket_expires_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,consumed_at INTEGER,
 PRIMARY KEY(app_id,frame_instance_id),
 FOREIGN KEY(app_id,subject) REFERENCES APPLICATION_ACCOUNTS(app_id,subject) ON DELETE CASCADE
);
"""


class ProfileFrames:
    def __init__(self, store):
        self.store = store

    async def issue(
        self, principal, parent_origin, embed_origin, ui_language, frame_instance_id
    ):
        from .profile import ApplicationProfileService

        async with self.store.transaction() as connection:
            await ApplicationProfileService(self.store)._account(connection, principal)
            _, config = await self.store._app(connection, principal.app_id)
            if (
                parent_origin not in config.parent_origins
                or embed_origin not in config.embed_origins
                or ui_language not in LANGUAGES
                or not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", frame_instance_id)
            ):
                raise EmbedError("invalid_request", 400)
            if await _one(
                connection,
                "SELECT 1 FROM APPLICATION_PROFILE_FRAMES WHERE app_id=? AND frame_instance_id=?",
                (principal.app_id, frame_instance_id),
            ):
                raise EmbedError("revision_conflict", 409)
            token = secrets.token_urlsafe(32)
            expiry = min(self.store.now() + 60, principal.expires_at)
            await connection.execute(
                "INSERT INTO APPLICATION_PROFILE_FRAMES VALUES (?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    principal.app_id,
                    principal.subject,
                    frame_instance_id,
                    principal.delegated_session_id,
                    _digest(token),
                    parent_origin,
                    embed_origin,
                    ui_language,
                    expiry,
                    principal.expires_at,
                ),
            )
            return {
                "contract_version": "aurvek_applications.v1",
                "frame_contract_version": "aurvek_embed.v1",
                "surface": "profile",
                "embed_url": embed_origin + "/embed/profile/bootstrap",
                "ticket": token,
                "expires_at": expiry,
                "frame_instance_id": frame_instance_id,
            }

    async def _principal(self, connection, row):
        from .profile import ApplicationProfileService

        principal = await self.store._session_identity(
            connection, row["delegated_session_id"]
        )
        if (principal.app_id, principal.subject) != (row["app_id"], row["subject"]):
            raise EmbedError("unauthenticated")
        await ApplicationProfileService(self.store)._account(connection, principal)
        return replace(
            principal,
            frame_instance_id=row["frame_instance_id"],
            parent_origin=row["parent_origin"],
            embed_origin=row["embed_origin"],
            ui_language=row["ui_language"],
            capabilities={},
        )

    async def consume(self, ticket, host, parent_origin):
        async with self.store.transaction() as connection:
            row = await _one(
                connection,
                "SELECT * FROM APPLICATION_PROFILE_FRAMES WHERE ticket_hash=?",
                (_digest(ticket),),
            )
            if (
                not row
                or row["consumed_at"] is not None
                or row["ticket_expires_at"] <= self.store.now()
                or row["parent_origin"] != parent_origin
                or urlsplit(row["embed_origin"]).netloc != host
            ):
                raise EmbedError("unauthenticated")
            principal = await self._principal(connection, row)
            await connection.execute(
                "UPDATE APPLICATION_PROFILE_FRAMES SET consumed_at=? WHERE ticket_hash=?",
                (self.store.now(), _digest(ticket)),
            )
            cookie = secrets.token_urlsafe(48)
            await connection.execute(
                "INSERT INTO EMBED_BROWSER_SESSIONS VALUES (?,?,?,?,?)",
                (
                    _digest(cookie),
                    principal.delegated_session_id,
                    row["embed_origin"],
                    principal.expires_at,
                    self.store.now(),
                ),
            )
            return cookie, principal

    async def resolve(self, cookie, host, app_id, frame_id):
        async with self.store.connection(readonly=True) as connection:
            browser = await self.store._browser(connection, cookie, host)
            row = await _one(
                connection,
                """SELECT * FROM APPLICATION_PROFILE_FRAMES WHERE app_id=? AND subject=?
                AND frame_instance_id=? AND delegated_session_id=? AND embed_origin=? AND consumed_at IS NOT NULL AND expires_at>?""",
                (
                    app_id,
                    browser.subject,
                    frame_id,
                    browser.delegated_session_id,
                    browser.embed_origin,
                    self.store.now(),
                ),
            )
            if not row or browser.app_id != app_id:
                raise EmbedError("not_found", 404)
            return await self._principal(connection, row)
