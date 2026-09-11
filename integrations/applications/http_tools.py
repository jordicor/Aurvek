"""Fixed operator endpoints; model arguments never select credentials or authority.

Responses are tool results for provider adapters, never assistant messages/SSE.
Mutation receipts deliberately survive transport errors and process interruption:
pending/ambiguous requests need operator reconciliation, not an automatic retry.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from jsonschema import Draft202012Validator
from pydantic import ConfigDict, Field, model_validator

from integrations.embed.models import EmbedError, StrictModel
from integrations.embed.store import _one
from .http_tools_schema import SCHEMA
from .models import ApplicationContext, AssistantConfig, LocalId


def _encoded(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _allowed_address(address, allow_private):
    if address.is_multicast or address.is_unspecified:
        return False
    if allow_private:
        return True
    embedded = getattr(address, "ipv4_mapped", None) or getattr(address, "sixtofour", None)
    return (address.is_global and not address.is_reserved
            and (embedded is None or embedded.is_global)
            and getattr(address, "teredo", None) is None)


class HTTPToolConfig(StrictModel):
    """Only trusted operators configure this model, never app clients or the LLM."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^app_http_[a-z][a-z0-9_]{0,47}$")
    description: str = Field(min_length=1, max_length=2000)
    url: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    effect: Literal["read", "write"] = "read"
    arguments_schema: dict[str, Any]
    assistant_ids: list[LocalId] = Field(min_length=1, max_length=64)
    enabled: bool = False
    credential_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    credential_header: Literal["Authorization", "X-API-Key"] = "Authorization"
    credential_prefix: Literal["Bearer ", ""] = "Bearer "
    allow_private_network: bool = False
    timeout_seconds: float = Field(default=10.0, ge=0.1, le=30)
    max_request_bytes: int = Field(default=65536, ge=256, le=262144)
    max_response_bytes: int = Field(default=65536, ge=256, le=262144)
    read_retries: int = Field(default=0, ge=0, le=1)
    supports_idempotency_key: bool = False

    @model_validator(mode="after")
    def validate_operator_config(self):
        parts = urlsplit(self.url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
                or parts.password or parts.fragment or parts.query or not self.url.isascii()
                or any(ord(c) <= 32 for c in self.url) or "\\" in self.url
                or "{" in self.url or "}" in self.url or parts.hostname.endswith(".")):
            raise ValueError("A fixed HTTP(S) URL without credentials, query or fragment is required")
        _ = parts.port
        if parts.scheme != "https" and not self.allow_private_network:
            raise ValueError("Public endpoints require HTTPS")
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is not None and not _allowed_address(address, self.allow_private_network):
            raise ValueError("Private endpoints require explicit operator permission")
        if self.effect == "write" and (self.method == "GET" or self.read_retries):
            raise ValueError("Writes cannot use GET or automatic retries")
        if len(_encoded(self.arguments_schema)) > 32768 or self.arguments_schema.get("type") != "object":
            raise ValueError("A bounded object JSON Schema is required")

        def check_refs(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                        raise ValueError("Schema references are not supported")
                    check_refs(value)
            elif isinstance(node, list):
                for value in node:
                    check_refs(value)

        check_refs(self.arguments_schema)
        Draft202012Validator.check_schema(self.arguments_schema)
        return self


class ApplicationHTTPToolService:
    def __init__(self, application_service, *, transport: httpx.AsyncBaseTransport | None = None):
        self.applications = application_service
        self.store = application_service.store
        self.transport = transport

    async def initialize(self):
        async with self.store.connection() as connection:
            await connection.executescript(SCHEMA)
            await connection.commit()

    async def configure_http_tool(self, app_id: str, config: HTTPToolConfig | dict):
        config = HTTPToolConfig.model_validate(config)
        async with self.store.transaction() as connection:
            await self.store._app(connection, app_id, active=False)
            for assistant in config.assistant_ids:
                if not await _one(connection, "SELECT 1 FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?",
                                  (app_id, assistant)):
                    raise ValueError("Assistant is not configured in this application")
            await connection.execute("""INSERT INTO APPLICATION_HTTP_TOOLS VALUES (?,?,?)
                ON CONFLICT(app_id,name) DO UPDATE SET config_json=excluded.config_json""",
                (app_id, config.name, config.model_dump_json()))

    async def _authorize(self, connection, context):
        if not isinstance(context, ApplicationContext):
            raise EmbedError("application_scope_mismatch", 403)
        live = await self.applications._validated_actor(connection, context)
        if not live.capabilities.get("tools"):
            raise EmbedError("capability_unavailable", 403)
        row = await _one(connection, "SELECT config_json FROM APPLICATION_ASSISTANTS WHERE app_id=? AND assistant_id=?",
                         (live.app_id, live.assistant_id))
        assistant = AssistantConfig.model_validate_json(row["config_json"])
        return live, assistant

    async def list_http_tools(self, context: ApplicationContext) -> list[dict]:
        async with self.store.connection(readonly=True) as connection:
            live, assistant = await self._authorize(connection, context)
            rows = await (await connection.execute("SELECT config_json FROM APPLICATION_HTTP_TOOLS WHERE app_id=? ORDER BY name",
                                                   (live.app_id,))).fetchall()
        configs = [HTTPToolConfig.model_validate_json(row["config_json"]) for row in rows]
        return [{"type": "function", "function": {"name": item.name, "description": item.description,
                 "parameters": item.arguments_schema}} for item in configs
                if item.enabled and live.assistant_id in item.assistant_ids and item.name in assistant.tools]

    async def run_http_tool(self, context: ApplicationContext, name: str, args: dict,
                            operation_id: str | None = None) -> dict:
        async with self.store.transaction() as connection:
            live, assistant = await self._authorize(connection, context)
            row = await _one(connection, "SELECT config_json FROM APPLICATION_HTTP_TOOLS WHERE app_id=? AND name=?",
                             (live.app_id, name))
            if not row:
                raise EmbedError("tool_unavailable", 403)
            config = HTTPToolConfig.model_validate_json(row["config_json"])
            if not config.enabled or live.assistant_id not in config.assistant_ids or name not in assistant.tools:
                raise EmbedError("tool_unavailable", 403)
            try:
                if type(args) is not dict or len(_encoded(args)) > config.max_request_bytes:
                    raise ValueError()
                if not Draft202012Validator(config.arguments_schema).is_valid(args):
                    raise ValueError()
            except (ValueError, TypeError, RecursionError):
                raise EmbedError("invalid_tool_arguments", 400) from None
            account_table = await _one(connection, "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APPLICATION_ACCOUNTS'")
            account = await _one(connection, "SELECT external_user_id FROM APPLICATION_ACCOUNTS WHERE app_id=? AND subject=?",
                                 (live.app_id, live.subject)) if account_table else None
            participant = await _one(connection, """SELECT context_ref FROM APPLICATION_PARTICIPANTS
                WHERE app_id=? AND context_id=? AND subject=?""", (live.app_id, live.context_id, live.subject))
            envelope = {"app_id": live.app_id, "actor": {"subject": live.subject,
                        "external_user_id": account["external_user_id"] if account else None},
                        "context": {"context_id": live.context_id, "context_ref": participant["context_ref"],
                                    "beneficiary_ref": live.beneficiary_ref},
                        "assistant_id": live.assistant_id, "conversation_id": live.conversation_id}
            payload = {"arguments": args, "aurvek": envelope}
            if len(_encoded(payload)) > config.max_request_bytes:
                raise EmbedError("tool_request_too_large", 400)
            digest = hashlib.sha256(_encoded({"config": config.model_dump(), "payload": payload})).hexdigest()
            if config.effect == "write":
                if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,128}", operation_id):
                    raise EmbedError("operation_id_required", 400)
                prior = await _one(connection, """SELECT * FROM APPLICATION_HTTP_OPERATIONS
                    WHERE app_id=? AND subject=? AND operation_id=?""", (live.app_id, live.subject, operation_id))
                if prior:
                    if prior["request_hash"] != digest:
                        raise EmbedError("operation_conflict", 409)
                    if prior["state"] == "succeeded":
                        return json.loads(prior["result_json"])
                    raise EmbedError("tool_operation_ambiguous", 409)
                await connection.execute("INSERT INTO APPLICATION_HTTP_OPERATIONS VALUES (?,?,?,?, 'pending',NULL,?)",
                                         (live.app_id, live.subject, operation_id, digest, self.store.now()))
        try:
            result = await self._request(config, payload, live, operation_id)
        except BaseException as error:
            if config.effect == "write":
                await self._finish(live, operation_id, "ambiguous")
                if isinstance(error, EmbedError):
                    raise EmbedError("tool_operation_ambiguous", 502) from None
            raise
        if config.effect == "write":
            await self._finish(live, operation_id, "succeeded", result)
        return result

    async def _finish(self, context, operation_id, state, result=None):
        async with self.store.transaction() as connection:
            await connection.execute("""UPDATE APPLICATION_HTTP_OPERATIONS SET state=?,result_json=?
                WHERE app_id=? AND subject=? AND operation_id=?""",
                (state, _encoded(result).decode() if result is not None else None,
                 context.app_id, context.subject, operation_id))

    async def _fixed_destination(self, config):
        url = httpx.URL(config.url)
        try:
            address = ipaddress.ip_address(url.host)
            addresses = [str(address)]
        except ValueError:
            infos = await asyncio.get_running_loop().getaddrinfo(url.host, url.port or (443 if url.scheme == "https" else 80),
                                                                type=socket.SOCK_STREAM)
            addresses = sorted({item[4][0] for item in infos})
        if not addresses or any(not _allowed_address(ipaddress.ip_address(ip), config.allow_private_network) for ip in addresses):
            raise EmbedError("tool_destination_denied", 403)
        # Connect to the validated IP itself, retaining TLS SNI and HTTP Host.
        return url.copy_with(host=addresses[0]), url

    async def _request(self, config, payload, context, operation_id):
        credential = os.environ.get(config.credential_env) if config.credential_env else None
        if config.credential_env and (not credential or len(credential) > 8192 or not credential.isascii()
                                      or any(ord(char) < 32 or ord(char) == 127 for char in credential)):
            raise EmbedError("tool_credential_unavailable", 503)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain",
                   "Accept-Encoding": "identity"}
        if credential:
            headers[config.credential_header] = config.credential_prefix + credential
        if config.effect == "write" and config.supports_idempotency_key:
            headers["Idempotency-Key"] = hashlib.sha256(_encoded([context.app_id, context.subject, operation_id])).hexdigest()
        attempts = 1 + (config.read_retries if config.effect == "read" else 0)
        try:
            async with asyncio.timeout(config.timeout_seconds):
                destination, original = await self._fixed_destination(config)
                headers["Host"] = original.netloc.decode("ascii")
                async with httpx.AsyncClient(transport=self.transport, timeout=config.timeout_seconds,
                                            follow_redirects=False, trust_env=False) as client:
                    for attempt in range(attempts):
                        try:
                            async with client.stream(config.method, destination, headers=headers, content=_encoded(payload),
                                extensions={"sni_hostname": original.host}) as response:
                                if not 200 <= response.status_code < 300:
                                    if config.effect == "read" and response.status_code >= 500 and attempt + 1 < attempts:
                                        continue
                                    raise EmbedError("tool_http_error", 502)
                                if response.headers.get("content-encoding", "identity").lower() != "identity":
                                    raise EmbedError("tool_invalid_response", 502)
                                data = bytearray()
                                async for chunk in response.aiter_bytes():
                                    data.extend(chunk)
                                    if len(data) > config.max_response_bytes:
                                        raise EmbedError("tool_response_too_large", 502)
                                text = data.decode("utf-8", errors="replace")
                                mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                                if mime == "application/json" or mime.endswith("+json"):
                                    try:
                                        value = json.loads(text, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                                    except (ValueError, RecursionError):
                                        raise EmbedError("tool_invalid_response", 502) from None
                                    return {"ok": True, "content_type": "json", "data": _redact(value, credential)}
                                if credential:
                                    text = text.replace(credential, "[REDACTED]")
                                return {"ok": True, "content_type": "text", "data": text}
                        except httpx.TransportError:
                            if attempt + 1 >= attempts:
                                raise
        except (httpx.HTTPError, OSError, TimeoutError):
            raise EmbedError("tool_transport_error", 502) from None


def _redact(value, credential):
    """Also handles JSON escaping of a credential accidentally echoed upstream."""
    if not credential:
        return value
    if isinstance(value, str):
        return value.replace(credential, "[REDACTED]")
    if isinstance(value, list):
        return [_redact(item, credential) for item in value]
    if isinstance(value, dict):
        return {_redact(key, credential): _redact(item, credential) for key, item in value.items()}
    return value
