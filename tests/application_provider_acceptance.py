"""Opt-in, isolated F4 acceptance against the real Claude runtime adapter.

Preparation (no network):
    python tests/application_provider_acceptance.py --model-id <local-LLM-id>
Real run, only after an operator has supplied ANTHROPIC_API_KEY in memory:
    python tests/application_provider_acceptance.py --model-id <id> --run-real \
        --report <new-report.json>

This is deliberately not named test_*. It starts no Aurvek application lifespan,
admin server or workers. A single temporary loopback HTTP fixture is closed before
exit. The actual model, tool executor, persistence and billing are never faked.
Semantic memory acceptance is separate; this runner explicitly disables memory.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
from contextlib import closing
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import tempfile
import time
import traceback
from urllib.parse import unquote, urlsplit


REPOSITORY = Path(__file__).resolve().parents[1]
MAX_PROVIDER_REQUESTS = 4
OUTPUT_TOKENS = 256
TOTAL_WALLET_USD = 1.0
MODEL_FIELDS = (
    "id", "machine", "model", "input_token_cost", "output_token_cost", "vision",
    "context_window_tokens", "max_input_tokens", "max_output_tokens", "enabled",
    "capabilities_json",
)
LOOKUP_NAME = "app_http_acceptance_lookup"
APP_IDS = ("acceptance-one", "acceptance-two")


class AcceptanceFailure(RuntimeError):
    """Messages must be static codes, never provider headers or response bodies."""


def require(condition, code):
    if not condition:
        raise AcceptanceFailure(code)


def diagnostic_code(value):
    """Keep machine codes, never arbitrary exception/provider prose."""
    if (isinstance(value, str) and 0 < len(value) <= 100
            and value[0].islower() and all(character in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in value)):
        return value
    return None


def summarize_runtime_events(events):
    allowed_keys = {"type", "content", "thinking", "error", "error_code", "terminal", "searching",
                    "application_handoff", "application_handoff_failed", "tool_call", "tool_call_pending",
                    "message_ids", "token_info", "input_tokens", "output_tokens", "citations", "search_queries"}
    shapes = {}
    failures = []
    error_codes = []
    tool_names = set()
    for event in events:
        keys = tuple(sorted(key if key in allowed_keys else "other" for key in event))
        event_type = diagnostic_code(event.get("type"))
        shape = (keys, event_type)
        shapes[shape] = shapes.get(shape, 0) + 1
        failure = event.get("application_handoff_failed")
        if isinstance(failure, dict):
            source = "error_code" if "error_code" in failure else "code"
            failures.append({"error_code": diagnostic_code(failure.get(source)), "source_field": source})
        code = diagnostic_code(event.get("error_code"))
        if code:
            error_codes.append(code)
        tool = event.get("tool_call")
        if isinstance(tool, dict) and tool.get("name") in {LOOKUP_NAME, "transfer_to_assistant", "web_search"}:
            tool_names.add(tool["name"])
    return {"event_count": len(events),
            "event_shapes": [{"keys": list(keys), "type": event_type, "count": count}
                             for (keys, event_type), count in shapes.items()],
            "application_handoff_failed": failures, "runtime_error_codes": error_codes,
            "runtime_tool_names": sorted(tool_names),
            "error_present": any(bool(event.get("error")) for event in events)}


class ObservedProviderContent:
    """Forward the original stream unchanged, recording protocol metadata only."""
    def __init__(self, original, diagnostics):
        self.original = original
        self.diagnostics = diagnostics

    def __getattr__(self, name):
        return getattr(self.original, name)

    async def __aiter__(self):
        async for line in self.original:
            try:
                if line.startswith(b"data: {"):
                    event = json.loads(line[6:])
                    event_type = event.get("type")
                    if event_type == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") in {"tool_use", "server_tool_use"}:
                            name = block.get("name")
                            self.diagnostics["tool_names"].append(
                                name if name in {LOOKUP_NAME, "transfer_to_assistant", "web_search"} else "other")
                    if event_type == "message_delta":
                        reason = event.get("delta", {}).get("stop_reason")
                        if reason in {"end_turn", "max_tokens", "stop_sequence", "tool_use", "pause_turn", "refusal"}:
                            self.diagnostics["stop_reason"] = reason
                            self.diagnostics["truncated"] = reason == "max_tokens"
                        tokens = event.get("usage", {}).get("output_tokens")
                        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
                            self.diagnostics["output_tokens"] = tokens
                    if event_type == "message_stop":
                        self.diagnostics["message_stop_received"] = True
            except (TypeError, ValueError, AttributeError):
                self.diagnostics["metadata_parse_errors"] = self.diagnostics.get("metadata_parse_errors", 0) + 1
            yield line


def read_public_model(source: Path, model_id: int) -> dict:
    """The sole source DB read: an explicit whitelist from LLM, read-only."""
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as connection, connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(LLM)")}
        fields = [field for field in MODEL_FIELDS if field in columns]
        row = connection.execute(
            "SELECT " + ",".join(fields) + " FROM LLM WHERE id=?", (model_id,)
        ).fetchone()
    require(row is not None, "public_model_not_found")
    model = dict(zip(fields, row))
    require(model["machine"] == "Claude", "runner_requires_native_claude_adapter")
    require(model.get("enabled", 1) == 1, "model_disabled")
    for field in ("input_token_cost", "output_token_cost"):
        value = float(model.get(field) or 0)
        require(math.isfinite(value) and value > 0, "model_pricing_missing")
    require(int(model.get("max_output_tokens") or OUTPUT_TOKENS) >= OUTPUT_TOKENS,
            "model_output_cap_too_small")
    model["max_output_tokens"] = OUTPUT_TOKENS
    return model


def install_sqlite_guard(root: Path):
    """Includes aiosqlite, whose worker creates connections through sqlite3."""
    original = sqlite3.connect

    def guarded(database, *args, **kwargs):
        raw = os.fspath(database)
        if raw != ":memory:":
            if raw.startswith("file:"):
                raw = unquote(raw[5:].split("?", 1)[0])
                if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
                    raw = raw[1:]
            require(Path(raw).resolve().is_relative_to(root), "database_outside_fixture_rejected")
        return original(database, *args, **kwargs)

    sqlite3.connect = guarded
    return original


class NetworkBoundary:
    """Real transport with a request ceiling and an exact destination allowlist."""

    def __init__(self, run_real):
        self.run_real = run_real
        self.requests = 0
        self.local_port = None
        self.failed = False
        self.provider_bodies = []
        self.provider_status = None
        self.provider_error_type = None
        self.allow_hosted_search = False
        self.provider_responses = []
        self.transfer_diagnostics = None
        self._allowed = contextvars.ContextVar("acceptance_network", default=False)

    def install(self):
        # Install before any Aurvek import. Redis and unrelated SDKs cannot open
        # sockets; permission exists only during the two guarded HTTP adapters.
        original_connect = socket.socket.connect
        original_connect_ex = socket.socket.connect_ex

        def connect(sock, address):
            require(self._allowed.get(), "unapproved_socket_rejected")
            return original_connect(sock, address)

        def connect_ex(sock, address):
            require(self._allowed.get(), "unapproved_socket_rejected")
            return original_connect_ex(sock, address)

        socket.socket.connect = connect
        socket.socket.connect_ex = connect_ex
        import aiohttp
        import httpx

        original_aiohttp = aiohttp.ClientSession._request
        original_httpx = httpx.AsyncClient.send

        async def aiohttp_request(session, method, url, **kwargs):
            require(self.run_real and not self.failed, "provider_execution_not_authorized")
            require(str(url) == "https://api.anthropic.com/v1/messages" and method.upper() == "POST",
                    "unexpected_aiohttp_destination")
            require(self.requests < MAX_PROVIDER_REQUESTS, "provider_request_ceiling_reached")
            data = kwargs.get("json") or {}
            require(0 < int(data.get("max_tokens", 0)) <= OUTPUT_TOKENS, "provider_output_cap_rejected")
            # Only native tools selected for this fixture may leave the process.
            offered = {item.get("name") for item in data.get("tools", [])}
            require(offered <= {LOOKUP_NAME, "transfer_to_assistant", "web_search"}, "unexpected_provider_tool")
            if "web_search" in offered:
                require(self.allow_hosted_search and self.requests == 3, "hosted_search_outside_destination_turn")
                search = [item for item in data["tools"] if item.get("name") == "web_search"]
                require(len(search) == 1 and search[0].get("type") == "web_search_20250305"
                        and search[0].get("max_uses") == 1, "hosted_search_limit_rejected")
            if self.allow_hosted_search:
                require("web_search" in offered, "native_hosted_search_not_offered")
            self.requests += 1
            self.provider_bodies.append({"output_cap": data["max_tokens"], "tool_names": sorted(offered)})
            kwargs["allow_redirects"] = False
            kwargs["timeout"] = aiohttp.ClientTimeout(total=90)
            token = self._allowed.set(True)
            try:
                response = await original_aiohttp(session, method, url, **kwargs)
                self.provider_status = response.status
                if response.status != 200:
                    self.failed = True  # Never retry failed real-provider work.
                else:
                    diagnostics = {"request": self.requests, "tool_names": [], "stop_reason": None,
                                   "truncated": None, "message_stop_received": False}
                    self.provider_responses.append(diagnostics)
                    response.content = ObservedProviderContent(response.content, diagnostics)
                return response
            except BaseException as error:
                self.failed = True
                self.provider_error_type = type(error).__name__
                raise
            finally:
                self._allowed.reset(token)

        async def httpx_send(client, request, **kwargs):
            parts = urlsplit(str(request.url))
            require(self.run_real and parts.scheme == "http" and parts.hostname == "127.0.0.1"
                    and parts.port == self.local_port and parts.path == "/synthetic-record",
                    "unexpected_httpx_destination")
            token = self._allowed.set(True)
            try:
                return await original_httpx(client, request, **kwargs)
            finally:
                self._allowed.reset(token)

        aiohttp.ClientSession._request = aiohttp_request
        httpx.AsyncClient.send = httpx_send


def configure_environment(root: Path, run_real: bool):
    # Root/operator explicitly supplies just the desired provider credential.
    # No dotenv is read and no credential is written into fixture DBs/artifacts.
    provider_key = os.environ.get("ANTHROPIC_API_KEY") if run_real else None
    require(not run_real or bool(provider_key), "ANTHROPIC_API_KEY_missing")
    for name in tuple(os.environ):
        if any(part in name.upper() for part in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
            os.environ.pop(name, None)
    os.environ.update({
        "DATABASE": "acceptance.sqlite", "ENVIRONMENT": "test", "APP_DEBUG": "false",
        "PEPPER": secrets.token_urlsafe(32), "APP_SECRET_KEY": secrets.token_urlsafe(32),
        "OPENAI_KEY": "synthetic-unused-key", "GEMINI_KEY": "synthetic-unused-key",
        "CLOUDFLARE_API_KEY": "synthetic-unused-key", "CLOUDFLARE_EMAIL": "synthetic@example.invalid",
        "CLOUDFLARE_ZONE_ID": "synthetic-unused-zone",
        "MEMORY_ACTIVE_PROVIDER": "none", "ATAGIA_ENABLED": "false",
        "READONLY_MODE": "false", "PYTHON_DOTENV_DISABLED": "1",
        "REDIS_HOST": "127.0.0.1", "REDIS_PORT": "1", "MAX_TOKENS": str(OUTPUT_TOKENS),
    })
    if provider_key:
        os.environ["ANTHROPIC_API_KEY"] = provider_key
    import dotenv
    dotenv.load_dotenv = lambda *args, **kwargs: False
    os.chdir(root)
    sys.path.insert(0, str(REPOSITORY))
    (root / "db").mkdir()
    # Imported logging remains completely silent, including provider exceptions.
    logging.disable(logging.CRITICAL)


def seed_native_schema(root: Path, model: dict):
    db = root / "db" / "acceptance.sqlite"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.executescript((REPOSITORY / "aurvek_schema.sql").read_text(encoding="utf-8"))
        connection.execute("CREATE TABLE IF NOT EXISTS SYSTEM_CONFIG (key TEXT PRIMARY KEY,value TEXT)")
        for key, value in {"memory_active_provider": "none", "atagia_enabled": "false",
                           "pricing_margin_free": "0", "pricing_margin_paid": "0",
                           "pricing_margin_personal": "0", "pricing_commission": "0",
                           "hosted_web_search_anthropic_max_uses": "1"}.items():
            connection.execute("INSERT OR REPLACE INTO SYSTEM_CONFIG(key,value) VALUES (?,?)", (key, value))
        # This isolated tariff exercises native SERVICE_USAGE reconciliation;
        # no artificial acceptance-only hold substitutes for provider billing.
        connection.execute("INSERT INTO SERVICES(name,unit,cost_per_unit,type) VALUES (?,?,?,?)",
                           ("hosted_web_search_anthropic", "search", .01, "Web Search"))
        fields = list(model)
        connection.execute("INSERT INTO LLM (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")",
                           [model[field] for field in fields])
        connection.execute("INSERT INTO USER_ROLES(id,role_name) VALUES (1,'admin'),(2,'user'),(3,'customer')")
        connection.execute("INSERT INTO USERS(id,username,role_id,is_enabled) VALUES (1,'synthetic-actor',3,1),(2,'synthetic-sponsor',3,1)")
        for prompt_id, name in ((10, "Reception"), (11, "Guide")):
            prompt = ("You are a synthetic acceptance assistant. Use exactly the named available function when the user "
                      "requests a lookup or transfer. After a lookup, answer naturally using only its result; never invent "
                      "the result. For transfer, call transfer_to_assistant with the requested existing conversation ID, "
                      "mode resume, note 'Synthetic test handoff', and a brief reply_message. Do not call unrelated tools.")
            if prompt_id == 11:
                prompt = ("You are the synthetic Guide assistant, in your own existing conversation. When asked to "
                          "search, use the native web_search tool exactly once and answer with one cited sentence "
                          "under 35 words. Do not invent sources or use another tool.")
            connection.execute("""INSERT INTO PROMPTS
                (id,name,prompt,public,created_by_user_id,forced_llm_id,disable_web_search,force_web_search)
                VALUES (?,?,?,1,2,?,?,?)""", (prompt_id, name, prompt, model["id"], int(prompt_id == 10), int(prompt_id == 11)))
        for user_id, balance in ((1, 0), (2, TOTAL_WALLET_USD)):
            connection.execute("""INSERT INTO USER_DETAILS
                (user_id,llm_id,current_prompt_id,balance,public_prompts_access,web_search_enabled,api_key_mode)
                VALUES (?,?,10,?,1,0,'system_only')""", (user_id, model["id"], balance))
        connection.commit()
    return db


async def prepare_applications(model):
    import database
    from auth import get_user_by_id
    from integrations.embed import identity
    from integrations.embed.models import AppConfig
    from integrations.embed.store import EmbedStore
    from integrations.applications import billing
    from integrations.applications.models import AssistantConfig, OpenConversationRequest
    from integrations.applications.service import ApplicationService
    from integrations.applications.http_tools import ApplicationHTTPToolService, HTTPToolConfig
    from ai_runtime.messages import get_ai_response

    async def unrevoked_synthetic_user(user_id):
        # The fixture has no Redis authentication service or preexisting sessions.
        require(user_id in (1, 2), "unknown_synthetic_identity")
        return False

    store = EmbedStore(database.get_db_connection, revoked_checker=unrevoked_synthetic_user)
    identity._store = store
    await store.initialize()
    applications = ApplicationService(store)
    await applications.initialize()
    capabilities = {"text": True, "tools": True, "web_search": True, "memory": False}
    user = await get_user_by_id(1)
    user.session_expires_at = int(time.time()) + 600
    conversations = {}
    for app_id in APP_IDS:
        config = AppConfig(app_id=app_id, display_name=app_id, issuer="https://aurvek.invalid",
            embed_origins=[f"https://chat.{app_id}.invalid"], parent_origins=[f"https://{app_id}.invalid"],
            redirect_uris=[f"https://{app_id}.invalid/callback"], prompt_id=10, enabled=True,
            capabilities=capabilities)
        await store.register_app(config, secrets.token_urlsafe(48))
        await store.provision_membership(app_id, 1, capabilities)
        await applications.register_assistants(app_id, [
            AssistantConfig(assistant_id="reception", display_name="Reception", prompt_id=10,
                tools=[LOOKUP_NAME, "transfer_to_assistant"], capabilities={**capabilities, "web_search": False}),
            AssistantConfig(assistant_id="guide", display_name="Guide", prompt_id=11,
                capabilities=capabilities),
        ], "reception")
        # Exercise the durable identity exchange with a newly synthesized native
        # identity. No native session from an existing account is copied.
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        transaction = await store.authorize_begin(app_id, config.redirect_uris[0], secrets.token_urlsafe(32), challenge, "en")
        code = await store.authorize_complete(transaction, user, secrets.token_urlsafe(48))
        tokens = await store.exchange_code(app_id, code["code"], verifier, code["redirect_uri"])
        actor = await store.inspect_access(app_id, tokens["delegated_credential"])
        await applications.grant_context(app_id, actor.subject, "synthetic-context", capabilities=capabilities)
        for assistant in ("reception", "guide"):
            state = await applications.open_conversation(actor, OpenConversationRequest(
                operation_id="acceptance-open-" + assistant, context_ref="synthetic-context", assistant_id=assistant))
            conversations[(app_id, assistant)] = state["conversation_id"]
        await billing.configure_funding(app_id, 2, monthly_limit=TOTAL_WALLET_USD)
    http = ApplicationHTTPToolService(applications)

    async def configure_lookup(port):
        for app_id in APP_IDS:
            await http.configure_http_tool(app_id, HTTPToolConfig(
                name=LOOKUP_NAME, description="Read the synthetic acceptance record's color.",
                url=f"http://127.0.0.1:{port}/synthetic-record", assistant_ids=["reception"],
                enabled=True, allow_private_network=True, read_retries=0,
                arguments_schema={"type": "object", "properties": {"record": {"type": "string", "enum": ["sample"]}},
                                  "required": ["record"], "additionalProperties": False}))

    await configure_lookup(1)
    from integrations.applications.tools import application_tool_catalog
    for app_id in APP_IDS:
        context, catalog = await application_tool_catalog(1, conversations[(app_id, "reception")], [])
        require(context.app_id == app_id, "application_scope_setup_failed")
        require({tool["function"]["name"] for tool in catalog} == {LOOKUP_NAME, "transfer_to_assistant"},
                "application_catalog_setup_failed")
    return applications, user, conversations, configure_lookup, get_ai_response


async def run_turn(applications, user, conversation_id, text, model, get_ai_response, *, context_messages=None):
    from ai_runtime.channel_turns import ChannelContext, bind_channel_turn
    from integrations.applications import activity, billing
    from billing.usage_reservations import user_billing_guard
    from ai_runtime.reasoning import ReasoningSelection
    from starlette.requests import Request

    scope = await applications.resolve_runtime_context(conversation_id, user.id)
    operation = await billing.admit_application_billing(scope)
    active = activity.begin_application_operation(operation)
    request = Request({"type": "http", "method": "POST", "path": "/synthetic-acceptance",
                       "headers": [], "query_string": b"", "scheme": "http",
                       "server": ("127.0.0.1", 1), "client": ("127.0.0.1", 1)})
    events = []

    async def consume():
        with bind_channel_turn(ChannelContext(application=operation.scope)):
            async for chunk in get_ai_response(message=text, context_messages=context_messages or [], conversation_id=conversation_id,
                machine=model["machine"], model=model["model"], current_user=user, request=request,
                max_tokens=OUTPUT_TOKENS, temperature=0, user_message=text, llm_id=model["id"],
                save_to_db=True, allow_gransabio=False, reasoning_selection=ReasoningSelection(mode="off"),
                input_token_cost_per_million=float(model["input_token_cost"]),
                output_token_cost_per_million=float(model["output_token_cost"])):
                if isinstance(chunk, bytes):
                    chunk = chunk.decode("utf-8")
                for line in chunk.splitlines():
                    if line.startswith("data: ") and line[6:] != "[DONE]":
                        try:
                            events.append(json.loads(line[6:]))
                        except json.JSONDecodeError:
                            raise AcceptanceFailure("invalid_runtime_sse") from None
        return events

    try:
        # The same account serialization used by the normal canonical entrypoint.
        with billing.binding_contextmanager(operation):
            async with user_billing_guard(user.id):
                return await consume()
    finally:
        activity.end_application_operation(active)


async def execute(root, model, boundary, run_real):
    boundary.install()  # The event loop's own wake-up sockets already exist.
    db = seed_native_schema(root, model)
    applications, user, conversations, configure_lookup, get_ai_response = await prepare_applications(model)
    try:
        sqlite3.connect(REPOSITORY / "db" / "Aurvek.db")
    except AcceptanceFailure:
        pass
    else:
        raise AcceptanceFailure("database_boundary_self_check_failed")
    report = {"kind": "aurvek-applications-f4-provider-acceptance", "real_provider": run_real,
              "model": {key: model.get(key) for key in ("id", "machine", "model")},
              "limits": {"provider_requests": MAX_PROVIDER_REQUESTS, "output_tokens": OUTPUT_TOKENS,
                         "total_synthetic_wallet_usd": TOTAL_WALLET_USD, "automatic_retries": 0},
              "semantic_memory_tested": False, "apps_prepared": 2, "native_conversations": 4,
              "checks": {"native_runtime_imported": True, "durable_scopes_and_tool_catalogs": True,
                         "foreign_database_connection_rejected": True}}
    if not run_real:
        report["status"] = "prepared_without_network"
        return report

    from aiohttp import web
    calls = []

    async def synthetic_record(request):
        payload = await request.json()
        require(payload["arguments"] == {"record": "sample"}, "unexpected_synthetic_arguments")
        calls.append(payload["aurvek"])
        return web.json_response({"record": "sample", "color": "violet", "synthetic": True})

    web_app = web.Application(client_max_size=4096)
    web_app.router.add_post("/synthetic-record", synthetic_record)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        boundary.local_port = site._server.sockets[0].getsockname()[1]
        await configure_lookup(boundary.local_port)
        source = conversations[(APP_IDS[0], "reception")]
        destination = conversations[(APP_IDS[0], "guide")]
        with closing(sqlite3.connect(db)) as connection, connection:
            connection.execute("INSERT INTO MESSAGES(conversation_id,user_id,message,type,date) "
                "VALUES (?,1,'Synthetic guide history marker','bot',CURRENT_TIMESTAMP)", (destination,))
        lookup_events = await run_turn(applications, user, source,
            "Call app_http_acceptance_lookup with record sample. Then tell me its color in one sentence.",
            model, get_ai_response)
        require(len(calls) == 1 and calls[0]["app_id"] == APP_IDS[0]
                and calls[0]["conversation_id"] == source, "lookup_did_not_reach_scoped_http_endpoint")
        require(not any(event.get("error") or event.get("terminal") == "persistence_error" for event in lookup_events),
                "lookup_runtime_failed")
        require(boundary.requests == 2, "lookup_requires_exactly_two_provider_requests")
        with closing(sqlite3.connect(db)) as connection, connection:
            bots = connection.execute("SELECT message FROM MESSAGES WHERE conversation_id=? AND type='bot' ORDER BY id", (source,)).fetchall()
        require(bots and "violet" in str(bots[-1][0]).lower(), "natural_answer_did_not_use_real_tool_result")
        transfer_events = await run_turn(applications, user, source,
            f"Transfer me now to assistant guide, resuming existing conversation {destination}. Use the transfer tool.",
            model, get_ai_response)
        # Preserve the actual runtime failure before the acceptance assertion.
        # No bot text, tool arguments, prompt or transport body enters the report.
        boundary.transfer_diagnostics = summarize_runtime_events(transfer_events)
        transferred = [event["application_handoff"] for event in transfer_events if "application_handoff" in event]
        require(len(transferred) == 1 and transferred[0].get("completed") is True
                and transferred[0]["conversation_id"] == destination
                and transferred[0]["source_conversation_id"] == source, "handoff_not_completed_to_existing_destination")
        require(boundary.requests == 3, "transfer_requires_one_provider_request")
        with closing(sqlite3.connect(db)) as connection, connection:
            destination_context = [{"id": row[0], "message": row[1], "type": row[2]} for row in connection.execute(
                "SELECT id,message,type FROM MESSAGES WHERE conversation_id=? ORDER BY id", (destination,))]
        require([item["message"] for item in destination_context] == ["Synthetic guide history marker"],
                "handoff_changed_existing_destination_history")
        boundary.allow_hosted_search = True
        search_events = await run_turn(applications, user, destination,
            "Use native web_search exactly once to find NASA's official James Webb Space Telescope mission page. "
            "Then give one sentence about the mission with a source citation, under 35 words.",
            model, get_ai_response, context_messages=destination_context)
        require(boundary.requests == 4, "destination_requires_one_provider_request")
        require(not any(event.get("error") or event.get("terminal") == "persistence_error" for event in search_events),
                "destination_search_runtime_failed_with_256_token_cap")
        citations = [citation for event in search_events if event.get("type") == "web_search_citations"
                     for citation in event.get("citations", []) if citation.get("url", "").startswith("https://")]
        require(bool(citations), "real_hosted_search_result_or_citation_missing")
        require(any(event.get("content", "").strip() for event in search_events), "destination_natural_answer_missing")
        with closing(sqlite3.connect(db)) as connection, connection:
            balances = dict(connection.execute("SELECT user_id,balance FROM USER_DETAILS"))
            reservations = connection.execute("SELECT status,SUM(COALESCE(settled_amount,0)) FROM BILLING_USAGE_RESERVATIONS GROUP BY status").fetchall()
            attribution = connection.execute("""SELECT o.app_id,o.payer_user_id,o.scope_json,r.status,r.purpose,r.settled_amount
                FROM APPLICATION_BILLING_OPERATIONS o JOIN APPLICATION_BILLING_RESERVATIONS a ON a.operation_id=o.operation_id
                JOIN BILLING_USAGE_RESERVATIONS r ON r.id=a.reservation_id""").fetchall()
            counts = dict(connection.execute("SELECT conversation_id,COUNT(*) FROM MESSAGES GROUP BY conversation_id"))
            handoffs = connection.execute("SELECT source_conversation_id,conversation_id,note_origin FROM APPLICATION_HANDOFFS").fetchall()
            destination_history = connection.execute("SELECT message FROM MESSAGES WHERE conversation_id=? ORDER BY id", (destination,)).fetchall()
            search_usage = connection.execute("""SELECT r.status,r.usage_quantity,r.settled_amount,s.cost_per_unit
                FROM BILLING_USAGE_RESERVATIONS r JOIN SERVICES s ON s.id=r.service_id
                WHERE r.purpose='web_search'""").fetchall()
        debit = TOTAL_WALLET_USD - float(balances[2])
        require(balances[1] == 0 and 0 < debit <= TOTAL_WALLET_USD, "sponsored_wallet_reconciliation_failed")
        require(attribution and all(row[0] == APP_IDS[0] and row[1] == 2
                and json.loads(row[2])["conversation_id"] in (source, destination)
                and row[3] == "settled" for row in attribution), "application_billing_attribution_failed")
        source_costs = [row for row in attribution if json.loads(row[2])["conversation_id"] == source]
        destination_costs = [row for row in attribution if json.loads(row[2])["conversation_id"] == destination]
        require(len(source_costs) == 2 and all(row[4] == "ai" for row in source_costs),
                "source_turns_or_transfer_costs_misattributed")
        require(sorted(row[4] for row in destination_costs) == ["ai", "web_search"],
                "destination_ai_and_hosted_search_costs_misattributed")
        require(len(search_usage) == 1 and search_usage[0][0] == "settled" and search_usage[0][1] == 1
                and abs(search_usage[0][2] - search_usage[0][3]) < 1e-12,
                "real_hosted_search_usage_not_natively_reconciled")
        require(all(status == "settled" for status, _ in reservations), "unsettled_provider_reservation")
        settled = sum(float(amount) for _, amount in reservations)
        require(abs(settled - debit) < 0.00000001, "wallet_and_reservations_disagree")
        require(counts.get(source) == 4 and len(destination_history) == 3
                and destination_history[0] == ("Synthetic guide history marker",)
                and str(destination_history[-1][0]).strip()
                and all(not counts.get(conversations[(APP_IDS[1], assistant)]) for assistant in ("reception", "guide")),
                "conversation_history_isolation_failed")
        require(len(handoffs) == 1, "duplicate_or_missing_handoff")
        from chat.services.stop_signals import stop_signals
        from integrations.applications.activity import get_active_operation
        require(not any(stop_signals.get(value) or get_active_operation(value) is not None
                        for value in conversations.values()), "post_handoff_activity_flags_not_clear")
        report.update(status="passed", provider_requests=boundary.requests, actual_debit_usd=debit,
                      settled_usd=settled, provider_request_limits=boundary.provider_bodies,
                      hosted_search_requests=search_usage[0][1], hosted_search_debit_usd=search_usage[0][2],
                      hosted_search_citations=len(citations),
                      source_debit_usd=sum(row[5] for row in source_costs),
                      destination_debit_usd=sum(row[5] for row in destination_costs))
        report["provider_responses"] = boundary.provider_responses
        report["transfer_diagnostics"] = boundary.transfer_diagnostics
        report["checks"].update(scoped_http_tool=True, natural_answer=True, transfer_to_existing_conversation=True,
            source_history_preserved=True, second_application_untouched=True, source_pays_transfer=True,
            destination_existing_history_preserved=True, destination_real_answer=True,
            native_hosted_search_result=True, native_hosted_search_charge=True,
            empty_actor_wallet_unchanged=True, all_reservations_settled=True, activity_and_stop_flags_clear=True)
        return report
    finally:
        await runner.cleanup()
        boundary.local_port = None


def failure_report(error, boundary, root, run_real):
    report = {"status": "failed", "real_provider": run_real,
              "error_type": type(error).__name__,
              "error_code": str(error) if isinstance(error, AcceptanceFailure) else "isolated_execution_failed",
              "provider_requests": boundary.requests,
              "provider_status": boundary.provider_status,
              "provider_error_type": boundary.provider_error_type,
              "provider_responses": boundary.provider_responses,
              "transfer_diagnostics": boundary.transfer_diagnostics,
              "failure_frames": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                                 for frame in traceback.extract_tb(error.__traceback__)[-5:]]}
    db = root / "db" / "acceptance.sqlite"
    if db.exists():
        with closing(sqlite3.connect(db)) as connection, connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "USER_DETAILS" in tables:
                report["synthetic_wallets"] = [list(row) for row in connection.execute("SELECT user_id,balance FROM USER_DETAILS ORDER BY user_id")]
            if "BILLING_USAGE_RESERVATIONS" in tables:
                report["reservation_states"] = [list(row) for row in connection.execute(
                    "SELECT status,COUNT(*),SUM(COALESCE(settled_amount,0)) FROM BILLING_USAGE_RESERVATIONS GROUP BY status")]
            if "MESSAGES" in tables:
                report["message_counts"] = [list(row) for row in connection.execute(
                    "SELECT conversation_id,type,COUNT(*) FROM MESSAGES GROUP BY conversation_id,type ORDER BY conversation_id,type")]
            if "APPLICATION_HANDOFFS" in tables:
                report["handoff_count"] = connection.execute("SELECT COUNT(*) FROM APPLICATION_HANDOFFS").fetchone()[0]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-id", type=int, required=True)
    parser.add_argument("--source-db", type=Path, default=REPOSITORY / "db" / "Aurvek.db")
    parser.add_argument("--run-real", action="store_true")
    parser.add_argument("--report", type=Path, help="New sanitized JSON report; never overwritten")
    args = parser.parse_args()
    output = args.report.resolve() if args.report else None
    require(not output or not output.exists(), "report_already_exists")
    # All source reads finish before runtime imports and the fixture-only guard.
    model = read_public_model(args.source_db, args.model_id)
    original_cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="aurvek-f4-acceptance-") as directory:
            root = Path(directory).resolve()
            configure_environment(root, args.run_real)
            install_sqlite_guard(root)
            boundary = NetworkBoundary(args.run_real)
            try:
                try:
                    report = asyncio.run(execute(root, model, boundary, args.run_real))
                except Exception as error:
                    report = failure_report(error, boundary, root, args.run_real)
            finally:
                os.chdir(original_cwd)
        require(boundary.local_port is None, "local_endpoint_not_closed")
    except Exception as error:
        # Avoid tracebacks/provider bodies that could contain credentials.
        report = {"status": "failed", "real_provider": args.run_real,
                  "error_type": type(error).__name__,
                  "error_code": str(error) if isinstance(error, AcceptanceFailure) else "isolated_execution_failed",
                  "provider_requests": getattr(locals().get("boundary"), "requests", 0),
                  "failure_frames": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                                     for frame in traceback.extract_tb(error.__traceback__)[-5:]],
                  "execution_result": locals().get("report")}
        os.chdir(original_cwd)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=True)
    print(json.dumps(report, indent=2, ensure_ascii=True))
    return 0 if report["status"] in {"passed", "prepared_without_network"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
