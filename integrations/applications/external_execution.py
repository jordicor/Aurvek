"""Application-owned text accounting through an authenticated control plane.

Aurvek keeps its native chat and immutable application operation. The external
application owns tariffs, reservations and customer debits. No provider keys or
conversation content leave Aurvek through this accounting protocol. BYOK secrets
remain in the existing encrypted store and are resolved only at dispatch.
"""
from __future__ import annotations

import asyncio
import json as json_codec
import os
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from database import get_db_connection
from integrations.embed.models import EmbedError

from .execution_protocol import (
    UsageStream,
    constrain_routing,
    describe,
    endpoint,
    rejected_evidence,
    usage_evidence,
)

_authorized_turn = ContextVar("external_authorized_turn", default=None)


@lru_cache(maxsize=1)
def profiles() -> dict:
    """Load operator-only application destinations once per process."""
    values = json_codec.loads(os.getenv("APPLICATION_EXTERNAL_EXECUTION", "{}"))
    if not isinstance(values, dict):
        raise TypeError("APPLICATION_EXTERNAL_EXECUTION must be an object")
    for value in values.values():
        if set(value) != {"url", "secret"} or len(value["secret"]) < 32:
            raise ValueError("External execution requires an exact URL and strong secret")
        parsed = urlsplit(value["url"])
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme not in {"https", "http"}
                or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1"})):
            raise ValueError("External execution requires HTTPS or loopback HTTP")
    return values


def current():
    """Resolve the frozen external mode; configuration removal never falls back."""
    from .billing import current_application_operation
    operation = current_application_operation()
    return operation if operation is not None and operation.mode == "external" else None


def reject_unsupported(capability: str) -> None:
    """Block paid helpers that do not yet emit authoritative attempt evidence."""
    if current() is not None:
        raise EmbedError(f"external_execution_unsupported_{capability}", 409)


async def control(operation, action: str, payload: dict) -> dict:
    """Make a bounded authenticated call with no provider or redirect retries."""
    from importlib.util import find_spec
    if find_spec("application_saas") is not None:
        from application_saas.chat import local_control, local_enabled
        if await local_enabled(operation.scope.app_id):
            return await local_control(operation, action, payload)
    profile = profiles().get(operation.scope.app_id)
    if profile is None:
        raise EmbedError("external_execution_not_configured", 503)
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.post(profile["url"].rstrip("/") + "/" + action,
            headers={"Authorization": f"Bearer {profile['secret']}"}, json=payload)
        if response.status_code != 200:
            raise EmbedError("external_execution_rejected", 409)
        return response.json()


async def open_turn(operation) -> dict:
    """Resolve native identity before requesting the external turn reservation."""
    from .billing import _one
    scope = operation.scope
    async with get_db_connection(readonly=True) as connection:
        account = await _one(connection,
            "SELECT external_user_id FROM APPLICATION_ACCOUNTS WHERE app_id=? AND subject=?",
            (scope.app_id, scope.subject))
        participant = await _one(connection,
            "SELECT context_ref FROM APPLICATION_PARTICIPANTS WHERE app_id=? AND subject=? AND context_id=?",
            (scope.app_id, scope.subject, scope.context_id))
        model = await _one(connection, "SELECT l.machine,l.model FROM CONVERSATIONS c JOIN LLM l ON l.id=c.llm_id WHERE c.id=? AND c.user_id=?",
                           (scope.conversation_id, scope.user_id))
    if not account or not participant or not model:
        raise EmbedError("external_execution_identity_missing", 403)
    return await control(operation, "open", {"app_id": scope.app_id,
        "external_user_id": account["external_user_id"], "context_ref": participant["context_ref"],
        "conversation_id": scope.conversation_id, "operation_id": operation.operation_id,
        "model_identity": {"GPT": "openai", "Claude": "anthropic", "Gemini": "gemini", "xAI": "xai", "OpenRouter": "openrouter"}.get(model["machine"], "unsupported") + ":" + model["model"]})


async def guarded_response(operation, awaitable):
    """Reserve before preflight and close in the native producer after persistence."""
    if operation.mode != "external":
        return await awaitable
    opened = None
    try:
        opened = await open_turn(operation)
        if opened["funding"] == "byok":
            opened["runtime_keys"] = {}
            for provider in opened["credential_bindings"]:
                opened["runtime_keys"][provider] = await _resolve_turn_key(operation, opened, provider)
    except BaseException:
        if hasattr(awaitable, "close"):
            awaitable.close()
        if opened is not None:
            await asyncio.shield(control(operation, "close", {"execution_id": opened["execution_id"]}))
        raise
    async def finish():
        await control(operation, "close", {"execution_id": opened["execution_id"]})
    token = _authorized_turn.set((operation.operation_id, opened))
    try:
        response = await awaitable
    except BaseException:
        await asyncio.shield(finish())
        raise
    finally:
        _authorized_turn.reset(token)
    body = getattr(response, "body_iterator", None)
    if body is None:
        await finish()
        return response
    async def stream():
        token = _authorized_turn.set((operation.operation_id, opened))
        try:
            async for item in body:
                yield item
        finally:
            try:
                await asyncio.shield(finish())
            finally:
                _authorized_turn.reset(token)
    response.body_iterator = stream()
    return response


async def _resolve_turn_key(operation, opened, provider):
    from integrations.embed.identity import get_embed_store

    from .provider_keys import ProviderKeyService, ResolveKey
    binding = opened["credential_bindings"].get(provider)
    if binding is None:
        raise EmbedError("external_execution_missing_provider_key", 409)
    return await ProviderKeyService(get_embed_store()).resolve(operation.scope.app_id,
        ResolveKey(organization_ref=opened["organization_id"], provider=provider,
                   credential_id=binding["credential_id"]))


def selected_api_key(provider: str):
    """Return the admitted key for native preflight without changing global settings."""
    bound = _authorized_turn.get()
    operation = current()
    if not bound or operation is None or bound[0] != operation.operation_id:
        raise EmbedError("external_execution_turn_not_admitted", 409)
    opened = bound[1]
    if opened["funding"] != "byok":
        return None
    key = opened.get("runtime_keys", {}).get("gemini" if provider == "google" else provider)
    if not key:
        raise EmbedError("external_execution_missing_provider_key", 409)
    return key


def _credential_headers(headers, provider, key):
    if key is None:
        return headers
    result = {name: value for name, value in headers.items()
              if name.lower() not in {"authorization", "x-api-key", "x-goog-api-key"}}
    name, value = ("x-api-key", key) if provider == "anthropic" else (
        ("x-goog-api-key", key) if provider == "gemini" else ("Authorization", f"Bearer {key}"))
    result[name] = value
    return result


async def admit_request(url: str, payload: dict):
    """Bind a physical call to the frozen turn before any provider I/O."""
    operation = current()
    bound = _authorized_turn.get()
    if operation is None or bound is None or bound[0] != operation.operation_id:
        raise EmbedError("external_execution_turn_not_admitted", 409)
    opened = bound[1]
    payload = dict(payload)
    provider, protocol, _ = endpoint(url)
    if provider == "openai":
        payload.setdefault("service_tier", "default")
    elif provider == "anthropic":
        payload.setdefault("service_tier", "standard_only")
    if provider == "openrouter":
        constrain_routing(payload, opened["routing_limits"])
    description = describe(url, payload)
    if f"{provider}:{description['model']}" not in opened["models"]:
        raise EmbedError("external_execution_unpriced_model", 409)
    from .billing import revalidate_application_operation
    await revalidate_application_operation(operation)
    key = await _resolve_turn_key(operation, opened, provider) if opened["funding"] == "byok" else None
    admitted = await control(operation, "attempt", {**description, "call_id": str(uuid.uuid4()),
                                                   "execution_id": opened["execution_id"]})
    report = {"execution_id": opened["execution_id"], "attempt_id": admitted["attempt_id"], "evidence": None}
    return operation, payload, protocol, report, key


async def retain_and_report(operation, report) -> None:
    """Persist evidence before callback; callback failure never frees funds."""
    async with get_db_connection() as connection:
        await connection.execute("INSERT INTO APPLICATION_EXTERNAL_EVIDENCE (attempt_id,operation_id,payload) VALUES (?,?,?)",
            (report["attempt_id"], operation.operation_id, json_codec.dumps(report)))
        await connection.commit()
    await control(operation, "record", report)
    async with get_db_connection() as connection:
        await connection.execute("UPDATE APPLICATION_EXTERNAL_EVIDENCE SET delivered=1 WHERE attempt_id=?", (report["attempt_id"],))
        await connection.commit()


class ObservedContent:
    """Observe native aiohttp streaming without changing its chunk or line interface."""

    def __init__(self, content, protocol):
        self.content = content
        self.observed = UsageStream(protocol)

    async def _iterate(self, stream):
        async for chunk in stream:
            self.observed.feed(chunk)
            yield chunk
        self.observed.finish()

    def iter_chunked(self, size):
        return self._iterate(self.content.iter_chunked(size))

    def iter_any(self):
        return self._iterate(self.content.iter_any())

    def __aiter__(self):
        return self._iterate(self.content.__aiter__())

    def __getattr__(self, name):
        return getattr(self.content, name)


class ObservedResponse:
    def __init__(self, response, protocol):
        self.response = response
        self.content = ObservedContent(response.content, protocol)

    def __getattr__(self, name):
        return getattr(self.response, name)


@asynccontextmanager
async def provider_request(session, url, *, headers, json):
    """Account the existing OpenAI, Claude, xAI and OpenRouter aiohttp dispatches."""
    if current() is None:
        async with session.post(url, headers=headers, json=json) as response:
            yield response
        return
    operation, payload, protocol, report, key = await admit_request(url, json)
    headers = _credential_headers(headers, endpoint(url)[0], key)
    observed = None
    try:
        async with session.post(url, headers=headers, json=payload, allow_redirects=False) as response:
            if response.status == 200 and payload.get("stream"):
                observed = ObservedResponse(response, protocol)
                yield observed
            else:
                report["evidence"] = (usage_evidence(await response.json(), protocol) if response.status == 200
                                      else rejected_evidence(response.status))
                yield response
    finally:
        if observed is not None and observed.content.observed.complete:
            report["evidence"] = observed.content.observed.evidence
        await asyncio.shield(retain_and_report(operation, report))


class AccountedGeminiTransport(httpx.AsyncBaseTransport):
    """Observe the Google SDK's physical HTTP requests on its supported injection seam."""

    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        operation, payload, protocol, report, key = await admit_request(str(request.url), json_codec.loads(await request.aread()))
        handed_off = False
        try:
            headers = _credential_headers(request.headers, "gemini", key)
            headers.pop("content-length", None)
            request = httpx.Request(request.method, request.url, headers=headers, json=payload, extensions=request.extensions)
            response = await self.transport.handle_async_request(request)
            if response.status_code != 200:
                report["evidence"] = rejected_evidence(response.status_code)
            elif protocol == "gemini_stream":
                response.stream = AccountedGeminiStream(response.stream, operation, report, protocol)
                handed_off = True
            else:
                report["evidence"] = usage_evidence(json_codec.loads(await response.aread()), protocol)
            return response
        finally:
            if not handed_off:
                await asyncio.shield(retain_and_report(operation, report))

    async def aclose(self):
        await self.transport.aclose()


class AccountedGeminiStream(httpx.AsyncByteStream):
    def __init__(self, stream, operation, report, protocol):
        self.stream, self.operation, self.report = stream, operation, report
        self.observed = UsageStream(protocol)
        self.recorded = False

    async def _record(self):
        if not self.recorded:
            if self.observed.complete:
                self.report["evidence"] = self.observed.evidence
            # Durable replay handles callback errors, so never insert this row twice.
            self.recorded = True
            await asyncio.shield(retain_and_report(self.operation, self.report))

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.observed.feed(chunk)
                yield chunk
            self.observed.finish()
        finally:
            await self._record()

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            await self._record()


def gemini_http_options():
    """Disable SDK retries and account each native Google generation request."""
    if current() is None:
        return None
    from google.genai import types
    return types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1),
        httpx_async_client=httpx.AsyncClient(transport=AccountedGeminiTransport(httpx.AsyncHTTPTransport()),
                                           timeout=300, follow_redirects=False))



async def replay_evidence(operation_id: str) -> int:
    """Replay saved callbacks only; never invoke a provider or reopen a turn."""
    from .billing import load_application_operation
    async with get_db_connection(readonly=True) as connection:
        operation = await load_application_operation(connection, operation_id)
        if operation.mode != "external":
            raise EmbedError("external_execution_operation_required", 409)
        cursor = await connection.execute(
            "SELECT attempt_id,payload FROM APPLICATION_EXTERNAL_EVIDENCE WHERE operation_id=? AND delivered=0 ORDER BY created_at,attempt_id",
            (operation_id,))
        rows = await cursor.fetchall()
    for row in rows:
        await control(operation, "record", json_codec.loads(row["payload"]))
        async with get_db_connection() as connection:
            await connection.execute("UPDATE APPLICATION_EXTERNAL_EVIDENCE SET delivered=1 WHERE attempt_id=?", (row["attempt_id"],))
            await connection.commit()
    return len(rows)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Replay retained application usage evidence without provider calls")
    parser.add_argument("--operation", required=True)
    arguments = parser.parse_args()
    print(asyncio.run(replay_evidence(arguments.operation)))
