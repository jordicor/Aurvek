"""Organization bindings over Aurvek's existing encrypted user credential store."""
from __future__ import annotations

import json
import secrets

import httpx
from pydantic import Field, SecretStr

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import _one

PROVIDERS = {"openai", "anthropic", "gemini", "xai", "openrouter", "typesafe"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS APPLICATION_PROVIDER_KEYS (
 app_id TEXT NOT NULL, organization_ref TEXT NOT NULL, provider TEXT NOT NULL,
 credential_id TEXT NOT NULL UNIQUE, external_user_id TEXT NOT NULL, label TEXT NOT NULL,
 masked_key TEXT NOT NULL, status TEXT NOT NULL, validated_at INTEGER, last_used_at INTEGER,
 updated_at INTEGER NOT NULL, PRIMARY KEY(app_id,organization_ref,provider),
 FOREIGN KEY(app_id,external_user_id) REFERENCES APPLICATION_ACCOUNTS(app_id,external_user_id)
);
"""


class KeyScope(StrictModel):
    organization_ref: str = Field(pattern=r"^[0-9a-f-]{36}$")


class KeyMutation(KeyScope):
    external_user_id: str = Field(min_length=1, max_length=128)
    provider: str = Field(pattern=r"^(openai|anthropic|gemini|xai|openrouter|typesafe)$")
    expected_credential_id: str | None = Field(default=None, max_length=64)


class SaveKey(KeyMutation):
    label: str = Field(min_length=1, max_length=80)
    api_key: SecretStr | None = None
    use_existing: bool = False


class ResolveKey(KeyScope):
    provider: str = Field(pattern=r"^(openai|anthropic|gemini|xai|openrouter|typesafe)$")
    credential_id: str = Field(min_length=16, max_length=64)


def _namespace(app_id: str, organization_ref: str, provider: str) -> str:
    return f"application:{app_id}:{organization_ref}:{provider}"


async def _read_keys(connection, user_id: int) -> dict:
    from common import decrypt_api_key
    row = await _one(connection, "SELECT user_api_keys FROM USER_DETAILS WHERE user_id=?", (user_id,))
    if row is None:
        raise EmbedError("provider_key_owner_unavailable", 409)
    if not row["user_api_keys"]:
        return {}
    value = decrypt_api_key(row["user_api_keys"])
    if not value:
        raise EmbedError("provider_key_store_unavailable", 503)
    return json.loads(value)


async def _write_keys(connection, user_id: int, values: dict) -> None:
    from common import encrypt_api_key
    encrypted = encrypt_api_key(json.dumps(values)) if values else None
    if values and not encrypted:
        raise EmbedError("provider_key_store_unavailable", 503)
    await connection.execute("UPDATE USER_DETAILS SET user_api_keys=? WHERE user_id=?", (encrypted, user_id))


class ProviderKeyService:
    """App-authenticated operations; the SaaS checks organization roles before calling."""

    def __init__(self, store):
        self.store = store

    async def _owner(self, connection, app_id, external_user_id, *, require_active=True):
        from .accounts import ApplicationAccountService
        binding = await ApplicationAccountService(self.store)._binding(connection, app_id, external_user_id)
        if require_active:
            await self.store._live(connection, app_id, binding["user_id"], binding["subject"])
        return binding["user_id"]

    async def _row(self, connection, app_id, body):
        return await _one(connection, """SELECT * FROM APPLICATION_PROVIDER_KEYS
            WHERE app_id=? AND organization_ref=? AND provider=?""",
            (app_id, body.organization_ref, body.provider))

    async def summaries(self, app_id: str, organization_ref: str) -> dict:
        """Return safe metadata only, including revoked references for display."""
        async with self.store.connection() as connection:
            rows = await (await connection.execute("""SELECT provider,credential_id,label,masked_key,status,
                validated_at,last_used_at,updated_at FROM APPLICATION_PROVIDER_KEYS
                WHERE app_id=? AND organization_ref=? ORDER BY provider""", (app_id, organization_ref))).fetchall()
        return {"keys": [dict(row) for row in rows]}

    async def save(self, app_id: str, body: SaveKey) -> dict:
        """Rotate an organization reference without creating another credential vault."""
        async with self.store.transaction() as connection:
            user_id = await self._owner(connection, app_id, body.external_user_id)
            previous = await self._row(connection, app_id, body)
            if (previous["credential_id"] if previous else None) != body.expected_credential_id:
                raise EmbedError("provider_key_changed", 409)
            values = await _read_keys(connection, user_id)
            if body.use_existing:
                if body.api_key is not None:
                    raise EmbedError("invalid_request", 400)
                key = values.get("google" if body.provider == "gemini" else body.provider)
            else:
                key = body.api_key.get_secret_value().strip() if body.api_key else None
            if not isinstance(key, str) or not 16 <= len(key) <= 4096 or any(c.isspace() for c in key):
                raise EmbedError("provider_key_required", 400)
            namespace = _namespace(app_id, body.organization_ref, body.provider)
            if previous and previous["external_user_id"] != body.external_user_id:
                old_id = await self._owner(connection, app_id, previous["external_user_id"], require_active=False)
                old_values = await _read_keys(connection, old_id)
                old_values.pop(namespace, None)
                await _write_keys(connection, old_id, old_values)
            values[namespace] = key
            await _write_keys(connection, user_id, values)
            reference = secrets.token_urlsafe(24)
            await connection.execute("""INSERT INTO APPLICATION_PROVIDER_KEYS
                (app_id,organization_ref,provider,credential_id,external_user_id,label,masked_key,status,updated_at)
                VALUES (?,?,?,?,?,?,?,'saved',?) ON CONFLICT(app_id,organization_ref,provider) DO UPDATE SET
                credential_id=excluded.credential_id,external_user_id=excluded.external_user_id,label=excluded.label,
                masked_key=excluded.masked_key,status='saved',validated_at=NULL,last_used_at=NULL,updated_at=excluded.updated_at""",
                (app_id, body.organization_ref, body.provider, reference, body.external_user_id,
                 body.label, "••••" + key[-4:], self.store.now()))
        return {"credential_id": reference, "status": "saved"}

    async def revoke(self, app_id: str, body: KeyMutation) -> dict:
        """Remove the secret; old executions keep attribution but cannot send again."""
        async with self.store.transaction() as connection:
            await self._owner(connection, app_id, body.external_user_id)
            row = await self._row(connection, app_id, body)
            if row is None or row["credential_id"] != body.expected_credential_id:
                raise EmbedError("provider_key_changed", 409)
            owner_id = await self._owner(connection, app_id, row["external_user_id"], require_active=False)
            values = await _read_keys(connection, owner_id)
            values.pop(_namespace(app_id, body.organization_ref, body.provider), None)
            await _write_keys(connection, owner_id, values)
            await connection.execute("UPDATE APPLICATION_PROVIDER_KEYS SET status='revoked',updated_at=? WHERE credential_id=?",
                                     (self.store.now(), row["credential_id"]))
        return {"status": "revoked"}

    async def resolve(self, app_id: str, body: ResolveKey, *, mark_used: bool = True) -> str:
        """Resolve one exact organization key for a trusted execution backend only."""
        async with self.store.transaction() as connection:
            row = await self._row(connection, app_id, body)
            if row is None or row["credential_id"] != body.credential_id or (row["status"] == "revoked" or (mark_used and row["status"] == "invalid")):
                raise EmbedError("provider_key_unavailable", 409)
            user_id = await self._owner(connection, app_id, row["external_user_id"])
            values = await _read_keys(connection, user_id)
            key = values.get(_namespace(app_id, body.organization_ref, body.provider))
            if not key:
                raise EmbedError("provider_key_unavailable", 409)
            if mark_used:
                await connection.execute("UPDATE APPLICATION_PROVIDER_KEYS SET last_used_at=? WHERE credential_id=?",
                                         (self.store.now(), body.credential_id))
            return key

    async def validate(self, app_id: str, body: ResolveKey) -> dict:
        """Check authentication with a read-only endpoint, never a generation."""
        key = await self.resolve(app_id, body, mark_used=False)
        urls = {"openai": "https://api.openai.com/v1/models", "anthropic": "https://api.anthropic.com/v1/models",
                "gemini": "https://generativelanguage.googleapis.com/v1beta/models", "xai": "https://api.x.ai/v1/models",
                "openrouter": "https://openrouter.ai/api/v1/key", "typesafe": "https://api.typesafe.ai/v1/models"}
        headers = {"Authorization": f"Bearer {key}"}
        if body.provider == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        elif body.provider == "gemini":
            headers = {"x-goog-api-key": key}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                response = await client.get(urls[body.provider], headers=headers)
            status = "ready" if response.status_code == 200 else "invalid" if response.status_code in {401, 403} else "unavailable"
        except httpx.HTTPError:
            status = "unavailable"
        async with self.store.transaction() as connection:
            await connection.execute("UPDATE APPLICATION_PROVIDER_KEYS SET status=?,validated_at=? WHERE credential_id=? AND status!='revoked'",
                                     (status, self.store.now(), body.credential_id))
        return {"status": status}
