"""Explicit conversational preferences owned by an application membership."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import _json, _one
from .models import OperationId


class ConversationalProfile(StrictModel):
    schema_version: Literal[1] = 1
    preferred_name: str | None = Field(default=None, max_length=120, strict=True)
    preferred_languages: list[str] = Field(default_factory=list, max_length=6)
    timezone_name: str | None = Field(default=None, max_length=100, strict=True)
    about_me: str | None = Field(default=None, max_length=4000, strict=True)

    @field_validator("preferred_languages")
    @classmethod
    def languages(cls, value):
        from user_languages import normalize_language_code

        return list(dict.fromkeys(normalize_language_code(code) for code in value))

    @field_validator("timezone_name")
    @classmethod
    def timezone(cls, value):
        from user_timezone import normalize_user_timezone

        return normalize_user_timezone(value)


class ConversationalProfileUpdate(StrictModel):
    operation_id: OperationId
    expected_version: int = Field(ge=1, strict=True)
    profile: ConversationalProfile


def profile_from_preferences(preferences, display_name=None):
    # Registration supplies a useful initial form of address. Once the profile
    # has been saved, an explicitly cleared name must stay cleared.
    values = preferences.get("conversational_profile") or {}
    if 'conversational_profile' not in preferences and display_name:
        values = {'preferred_name': display_name}
    return ConversationalProfile.model_validate(values)


async def load_profile(connection, app_id, subject):
    row = await _one(
        connection,
        "SELECT preferences_json,display_name FROM APPLICATION_ACCOUNTS WHERE app_id=? AND subject=?",
        (app_id, subject),
    )
    return (
        profile_from_preferences(json.loads(row["preferences_json"]), row['display_name'])
        if row
        else ConversationalProfile()
    )


async def conversation_profile(connection, conversation_id):
    """None means native; an empty profile is still an application boundary."""
    # Native-only installations and narrow fixtures need no application schema.
    if not await _one(
        connection,
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_CONVERSATIONS'",
    ):
        return None
    row = await _one(
        connection,
        "SELECT app_id,subject FROM APPLICATION_CONVERSATIONS WHERE conversation_id=?",
        (conversation_id,),
    )
    return (
        await load_profile(connection, row["app_id"], row["subject"]) if row else None
    )


def personal_context(profile):
    # This is user data, never system authority. JSON preserves explicit field boundaries.
    if not profile.preferred_name and not profile.about_me:
        return None
    return "Application conversational profile (user-provided data):\n" + _json(
        {"preferred_name": profile.preferred_name, "about_me": profile.about_me}
    )


class ApplicationProfileService:
    def __init__(self, store):
        self.store = store

    async def _account(self, connection, principal):
        from .accounts import ApplicationAccountService

        principal = await ApplicationAccountService(self.store)._identity(
            connection, principal
        )
        row = await _one(
            connection,
            """SELECT a.*,s.user_id FROM APPLICATION_ACCOUNTS a
            JOIN EMBED_SUBJECTS s USING(subject) WHERE a.app_id=? AND a.subject=?""",
            (principal.app_id, principal.subject),
        )
        if not row:
            raise EmbedError("not_found", 404)
        return row

    async def _state(self, connection, row):
        from .contacts import contact_state

        contact = await contact_state(connection, row["app_id"], row["subject"])
        receivers = await (
            await connection.execute(
                """SELECT r.receiver_id,r.channel,r.enabled,
            r.receiver_key,l.link_id,l.active,l.version FROM APPLICATION_CHANNEL_RECEIVERS r
            LEFT JOIN APPLICATION_CHANNEL_LINKS l ON l.receiver_id=r.receiver_id AND l.subject=?
                AND ((r.channel IN ('phone','whatsapp') AND l.provider_identity=?)
                    OR (r.channel='telegram' AND EXISTS (SELECT 1 FROM APPLICATION_TELEGRAM_CONTACT_PROOFS p
                        WHERE p.link_id=l.link_id AND p.phone_number=?)))
            WHERE r.app_id=?""",
                (
                    row["subject"],
                    contact["phone_number"],
                    contact["phone_number"],
                    row["app_id"],
                ),
            )
        ).fetchall()
        channels = []
        for channel in ("whatsapp", "phone", "telegram"):
            items = [
                dict(item)
                for item in receivers
                if item["channel"] == channel and item["enabled"]
            ]
            channels.append(
                {
                    "channel": channel,
                    "status": (
                        "connected"
                        if contact["verified"] and any(item["active"] for item in items)
                        else "available"
                        if contact["verified"] and channel != "telegram"
                        else "connection_required"
                    )
                    if items
                    else "configuration_pending",
                    "receivers": [
                        {
                            **{
                                key: item[key]
                                for key in (
                                    "receiver_id",
                                    "receiver_key",
                                    "link_id",
                                    "version",
                                )
                            },
                            "connected": bool(item["active"]) and contact["verified"],
                        }
                        for item in items
                    ],
                }
            )
        return {
            "version": row["version"],
            "profile": profile_from_preferences(
                json.loads(row["preferences_json"]), row['display_name']
            ).model_dump(),
            "contact": contact,
            "channels": channels,
        }

    async def state(self, principal):
        async with self.store.connection(readonly=True) as connection:
            return await self._state(
                connection, await self._account(connection, principal)
            )

    async def update(self, principal, body):
        from .accounts import ApplicationAccountService, AccountOperation

        accounts = ApplicationAccountService(self.store)
        async with self.store.transaction() as connection:
            row = await self._account(connection, principal)
            # Use the existing account operation ledger, with the full typed body.
            import hashlib

            digest = hashlib.sha256(
                _json(
                    {"kind": "conversational_profile", "body": body.model_dump()}
                ).encode()
            ).hexdigest()
            prior = await _one(
                connection,
                """SELECT request_hash,result_json FROM APPLICATION_ACCOUNT_OPERATIONS
                WHERE app_id=? AND external_user_id=? AND operation_id=?""",
                (principal.app_id, row["external_user_id"], body.operation_id),
            )
            if prior:
                if prior["request_hash"] != digest:
                    raise EmbedError("idempotency_conflict", 409)
                return json.loads(prior["result_json"])
            if row["version"] != body.expected_version:
                raise EmbedError("version_conflict", 409)
            preferences = json.loads(row["preferences_json"])
            preferences["conversational_profile"] = body.profile.model_dump()
            from .accounts import LocalProfile

            LocalProfile(preferences=preferences)
            await connection.execute(
                """UPDATE APPLICATION_ACCOUNTS SET preferences_json=?,version=version+1,updated_at=?
                WHERE app_id=? AND subject=?""",
                (
                    _json(preferences),
                    self.store.now(),
                    principal.app_id,
                    principal.subject,
                ),
            )
            result = await self._state(
                connection, await self._account(connection, principal)
            )
            return await accounts._save(
                connection,
                principal.app_id,
                AccountOperation(
                    operation_id=body.operation_id,
                    external_user_id=row["external_user_id"],
                ),
                digest,
                result,
            )
