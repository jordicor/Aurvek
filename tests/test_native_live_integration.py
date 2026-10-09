"""Live delegation through canonical message/tool dispatch, without network I/O.

SQLite, tool policy, KB retrieval, and provider-native continuations are real.
Authentication/scope lookup, unrelated memory, billing settlement, and provider
responses are fixtures. These tests do not establish audible playback or cost.
"""

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import sqlite3
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from ai_runtime import messages
from ai_runtime.channel_turns import ChannelContext, current_channel_turn
from ai_runtime.memory.context import MemoryContextDecision
from ai_runtime.persistence import messages as persistence
from help_schema import ensure_help_schema
from integrations.live.session import LiveMediaSession


ROOT = Path(__file__).resolve().parents[1]
HELP_QUERY = "synthetic password"
WEB_QUERY = "synthetic current release"
WEB_RESULT = "Synthetic release 7 was published today. Source: https://example.test/releases/7"


class TestUser:
    __test__ = False
    id = 1
    can_send_files = False
    can_generate_images = False
    is_enabled = True
    ui_language = "en"

    @property
    async def is_admin(self):
        return True  # Stale identity: KB must use the current database role.

    @property
    async def is_user(self):
        return True


@pytest.fixture
def live_backend(tmp_path, monkeypatch):
    import common
    import database
    from ai_runtime.context import assembly
    from integrations.applications import tools as app_tools
    from integrations.applications import runtime as app_runtime
    from integrations.devices import service as devices
    from integrations.embed import runtime as embed_runtime

    path = tmp_path / "integration.db"
    with sqlite3.connect(path) as db:
        db.executescript((ROOT / "aurvek_schema.sql").read_text(encoding="utf-8"))
        db.execute("ALTER TABLE CONVERSATIONS ADD COLUMN is_incognito INTEGER DEFAULT 0")
        ensure_help_schema(db)
        db.execute("INSERT INTO USER_ROLES(id,role_name) VALUES(1,'customer'),(2,'admin')")
        db.execute("INSERT INTO USERS(id,username,role_id,is_enabled) VALUES(1,'synthetic',1,1)")
        db.execute("INSERT INTO USER_DETAILS(user_id,balance,api_key_mode,web_search_mode) VALUES(1,100,'system_only','perplexity')")
        db.execute("INSERT INTO PROMPTS(id,name,prompt,realtime_llm_id,realtime_backend_llm_id) VALUES(1,'Synthetic assistant','Use tools to verify the answer.',9,1)")
        db.execute("INSERT INTO LLM(id,machine,model,input_token_cost,output_token_cost,max_output_tokens,capabilities_json) VALUES(1,'GPT','gpt-4.1',1,2,512,'{}')")
        db.execute("INSERT INTO LLM(id,machine,model) VALUES(9,'Gemini','gemini-3.8-live'),(10,'GPTSub','gpt-5.6-luna')")
        db.execute("UPDATE LLM SET enabled=0 WHERE id=10")
        db.execute("INSERT INTO CONVERSATIONS(id,user_id,role_id,llm_id,locked,chat_name) VALUES(1,1,1,10,0,'Synthetic acceptance')")
        for scope, role, marker in (
            ("platform", None, "PLATFORM_ALLOWED"),
            ("platform", "admin", "ADMIN_DENIED"),
            ("app:alpha", None, "ALPHA_ALLOWED"),
            ("app:beta", None, "BETA_DENIED"),
            ("embed", None, "SHARED_ALLOWED"),
        ):
            db.execute(
                "INSERT INTO HELP_ARTICLES(article_id,title,category,keywords,short_answer,tool_text,body,"
                "tool_visible,approval_status,required_role,knowledge_scope) VALUES(?,?,?,?,?,?,?,1,'approved',?,?)",
                (marker.lower(), "Synthetic password guide", "qa", '["password"]', marker, marker, marker, role, scope),
            )

    @asynccontextmanager
    async def connection(*_args, **_kwargs):
        async with aiosqlite.connect(path) as db:
            db.row_factory = aiosqlite.Row
            yield db

    env = SimpleNamespace(path=path, scope=None, provider_calls=[], tool_results=[], saved=[],
                          usage=[], settlements=[],
                          query_logged=asyncio.Event(), connection=connection)
    service = SimpleNamespace(
        resolve_runtime_context=AsyncMock(side_effect=lambda *_args: env.scope),
        store=SimpleNamespace(connection=connection),
    )
    monkeypatch.setattr(app_tools, "_services", AsyncMock(return_value=service))
    monkeypatch.setattr(app_runtime, "authorize_application_read", AsyncMock(side_effect=lambda *_args, **_kw: env.scope))
    monkeypatch.setattr(embed_runtime, "is_embed_conversation", AsyncMock(return_value=False))
    for module in (messages, common, database):
        monkeypatch.setattr(module, "get_db_connection", connection)
    monkeypatch.setattr(messages, "ensure_conversation_privacy_schema", AsyncMock())
    monkeypatch.setattr(messages, "validate_message_request", AsyncMock(return_value=None))
    monkeypatch.setattr(devices, "load_user_api_keys", AsyncMock(return_value={}))
    monkeypatch.setattr(messages, "get_effective_blocks", AsyncMock(return_value={}))
    monkeypatch.setattr(messages, "_resolve_memory_context", AsyncMock(
        side_effect=lambda prompt, **_kw: MemoryContextDecision(prompt, False, "disabled")))
    monkeypatch.setattr(messages, "apply_no_memory_context_budget", AsyncMock(side_effect=lambda context, **_kw: context))
    monkeypatch.setattr(assembly, "load_user_time_context", AsyncMock(return_value=None))
    monkeypatch.setattr(assembly, "load_user_language_context", AsyncMock(return_value=None))
    monkeypatch.setattr(messages, "get_user_billing_availability", AsyncMock(return_value={"available": 100}))
    monkeypatch.setattr(messages, "get_variable_billing_rates", AsyncMock(
        return_value=SimpleNamespace(input_per_token=.000001, output_per_token=.000002)))
    monkeypatch.setattr(messages, "revalidate_user_billing", AsyncMock(return_value=True))
    monkeypatch.setattr(messages, "reserve_ai_usage", AsyncMock(return_value="synthetic-hold"))
    monkeypatch.setattr(messages, "extend_ai_reservation", AsyncMock())
    monkeypatch.setattr(messages, "_settle_or_refund_ai_reservation", AsyncMock())
    from billing import usage_reservations

    async def accumulate(**kwargs):
        env.usage.append(kwargs)

    monkeypatch.setattr(usage_reservations, "accumulate_ai_reservation_usage", accumulate)

    async def settle(content, *_args, **kwargs):
        assert kwargs["save_assistant_message"] is False
        assert kwargs["user_message"] is None
        env.saved.append(content)
        env.settlements.append(kwargs)
        kwargs["on_database_commit"]((None, None))
        return None, None

    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", settle)
    from tools import platform_help
    original_lookup, original_log = platform_help.lookup_platform_help, platform_help.log_help_query

    async def lookup(*args, **kwargs):
        result = await original_lookup(*args, **kwargs)
        env.tool_results.append(result)
        return result

    async def log_query(*args):
        await original_log(*args)
        env.query_logged.set()

    monkeypatch.setattr(platform_help, "lookup_platform_help", lookup)
    monkeypatch.setattr(platform_help, "log_help_query", log_query)
    env.service = service
    return env


def application_scope(env, monkeypatch, *, allow_tools=True, allow_search=False):
    """Supply an admitted account; keep tool grants and KB filters real."""
    from integrations.applications import billing, handoff, http_tools
    from integrations.applications.models import ApplicationContext
    from ai_runtime.tooling import context_budget
    env.scope = ApplicationContext(app_id="alpha", subject="synthetic", user_id=1,
        context_id="workspace", assistant_id="default", prompt_id=1, conversation_id=1,
        membership_version=1, capabilities={"text": True, "voice": True, "stt": True, "tts": True,
                                          "tools": allow_tools, "web_search": allow_search})
    env.operation = billing.ApplicationBillingOperation("synthetic-operation", env.scope,
        "synthetic-grant", 1, 1, "native", "2026-09", None)
    env.service._scope = AsyncMock(return_value=(SimpleNamespace(
        tools=["lookup_platform_help", "query_perplexity"], max_tool_rounds=1), None, None, None))
    monkeypatch.setattr(billing, "revalidate_application_operation", AsyncMock())
    monkeypatch.setattr(handoff.ApplicationHandoffService, "state", AsyncMock(return_value={"assistants": []}))
    monkeypatch.setattr(handoff, "handoff_context_message", AsyncMock(return_value=None))
    monkeypatch.setattr(http_tools.ApplicationHTTPToolService, "list_http_tools", AsyncMock(return_value=[]))
    monkeypatch.setattr(context_budget, "model_input_token_limit", AsyncMock(return_value=128000))


def scripted_backend(env, monkeypatch, *, tool="lookup_platform_help", arguments=None, answer=None,
                     followup_tool=None, on_call=None, terminal_event=None):
    """Fake the text provider, retaining the real tool dispatch and result sink."""
    arguments = arguments if arguments is not None else {"query": HELP_QUERY, "locale": "en"}

    async def provider(**kwargs):
        env.provider_calls.append(copy.deepcopy({key: kwargs.get(key) for key in (
            "messages", "model", "tools", "web_search_mode", "tool_choice", "billing_reservation_id")}))
        bound = current_channel_turn()
        assert bound.context.persistence == "delegation"
        assert bound.context.input_perception == "transcript_only"
        if on_call is not None:
            await on_call(bound, kwargs)
        from billing.usage_reservations import accumulate_ai_provider_call_usage
        call_number = len(env.provider_calls)
        call_input, call_output = await accumulate_ai_provider_call_usage(
            reservation_id=kwargs["billing_reservation_id"], user_id=1,
            reported_input_tokens=10 * call_number, reported_output_tokens=4 * call_number,
            input_payload=kwargs["messages"], output_payload=tool, output_token_cap=512,
            llm_id=1, model="gpt-4.1", prompt_id=1)
        if len(env.provider_calls) == 1:
            yield "data: " + json.dumps({"tool_call": {"name": tool, "arguments": arguments,
                "id": "synthetic-call", "_billing_usage": {"input_tokens": call_input,
                                                           "output_tokens": call_output}}}) + "\n\n"
            yield 'data: {"tool_call_pending":true}\n\n'
            return
        assert len(env.provider_calls) == 2
        result = next(item["output"] for item in kwargs["messages"] if item.get("type") == "function_call_output")
        text = answer if answer is not None else result
        if followup_tool is not None:
            yield "data: " + json.dumps({"tool_call": {"name": followup_tool,
                "arguments": {"final_message": text + " Goodbye."}, "id": "synthetic-close",
                "_billing_usage": {"input_tokens": call_input, "output_tokens": call_output}}}) + "\n\n"
            yield 'data: {"tool_call_pending":true}\n\n'
            if terminal_event is not None:
                yield "data: " + json.dumps(terminal_event) + "\n\n"
            return
        await persistence.save_content_to_db(text, call_input, call_output, call_input + call_output,
                                            1, 1, "gpt-4.1", prompt_id=1, llm_id=1,
                                            billing_reservation_id=kwargs["billing_reservation_id"],
                                            billing_only_accumulated_usage=True)
        yield "data: " + json.dumps({"content": text, "delegation_complete": True}) + "\n\n"

    monkeypatch.setattr(messages, "call_gpt_responses_api", provider)


def scripted_search(env, monkeypatch, *, fail=False, settled=True):
    """Keep the metered search service real; fake transport and ledger I/O."""
    from tools import perplexity
    result = perplexity._parse_perplexity_response({
        "choices": [{"message": {"content": WEB_RESULT}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 12},
    }, perplexity._research_payload(WEB_QUERY))
    search = AsyncMock(side_effect=RuntimeError("Synthetic upstream failure") if fail else None,
                       return_value=result)
    monkeypatch.setattr(perplexity, "PERPLEXITY_API_KEY", "synthetic-search-test")
    monkeypatch.setattr(perplexity, "_get_perplexity_result", search)
    for name, value in (("estimate_customer_charge_from_api_cost", .1),
                        ("reserve_ai_usage", "synthetic-search-hold"),
                        ("mark_ai_reservation_provider_started", True),
                        ("accumulate_ai_reservation_usage", None),
                        ("settle_ai_reservation_components", settled), ("refund_fixed_usage", None)):
        monkeypatch.setattr(perplexity, name, AsyncMock(return_value=value))
    env.search_usage = perplexity.accumulate_ai_reservation_usage
    return search


async def delegate(env, *, channel="web", kind="gemini_live", end_requested=False):
    session = LiveMediaSession(SimpleNamespace(send_json=AsyncMock()), conversation_id=1,
        user=TestUser(), request=None, check_identity=AsyncMock(), channel=channel)
    completed = asyncio.Event()
    env.spoken_result = []

    async def commentary(text, **kwargs):
        env.spoken_result.append((text, kwargs["delegation_id"]))
        completed.set()

    session.client = SimpleNamespace(_aurvek_internal_realtime_bridge=True, commentary=commentary)
    from integrations.realtime.configuration import resolve_selection
    async with env.connection() as db:
        await db.execute("UPDATE LLM SET machine=?,model=? WHERE id=9", (
            "GPT" if kind == "openai_live" else "Gemini", "gpt-live-1" if kind == "openai_live" else "gemini-3.8-live"))
        await db.commit()
        chat_id = (await (await db.execute("SELECT llm_id FROM CONVERSATIONS WHERE id=1")).fetchone())[0]
        selection = await resolve_selection(db, prompt_id=1, conversation_llm_id=chat_id,
                                            channel="device" if channel == "device" else "live")
    session.settings = {"kind": kind, "conversation": {"llm_id": chat_id},
                        "backend": selection.backend, "selection": selection}
    session._active = True
    session.foreground = SimpleNamespace(context=ChannelContext(channel=channel, application=env.scope))
    session.check_access = AsyncMock()
    session._persist = AsyncMock()
    request = "Check the synthetic documentation and report the verified result."
    if end_requested:
        request += " Then say goodbye and end this live session."
    session.recent = [("user", request)]
    await session._provider_event({"type": "session.delegation.created",
        "delegation": {"id": "live-delegation", "request": request}})
    from integrations.applications.billing import binding_contextmanager
    with binding_contextmanager(env.operation) if env.scope is not None else nullcontext():
        worker = asyncio.create_task(session._tools())
    waiter = asyncio.create_task(completed.wait())
    try:
        done, _ = await asyncio.wait((worker, waiter), timeout=5, return_when=asyncio.FIRST_COMPLETED)
        if worker in done:
            await worker
        assert completed.is_set(), "Live delegation did not return a result"
    finally:
        worker.cancel()
        waiter.cancel()
        await asyncio.gather(worker, waiter, return_exceptions=True)
    return session


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["missing", "forged", "canceled", "wrong_backend"])
async def test_native_backend_override_rejects_untrusted_or_unconfigured_choices(live_backend, authorization):
    from dataclasses import replace
    from ai_runtime.channel_turns import VoiceDelegationResult
    from integrations.live.control import LiveSessionControl
    from integrations.realtime.configuration import resolve_selection

    env = live_backend
    session = LiveMediaSession(None, conversation_id=1, user=TestUser(), request=None,
                               check_identity=AsyncMock())
    session.client = SimpleNamespace(_aurvek_internal_realtime_bridge=True)
    session._active = True
    session.check_access = AsyncMock()
    async with env.connection() as db:
        selection = await resolve_selection(db, prompt_id=1, conversation_llm_id=10, channel="live")
    session.settings = {"selection": selection}
    control = LiveSessionControl(session, "synthetic-override")
    session._tool_control = control
    if authorization == "canceled":
        control.canceled = True
    provenance = {"native_audio_bridge": session.client}
    if authorization != "missing":
        provenance["live_session_control"] = (SimpleNamespace(matches=lambda **_: True)
                                               if authorization == "forged" else control)
    context = replace(ChannelContext(), persistence="delegation", delegation_result=VoiceDelegationResult(),
                      input_origin="web.live_voice", input_perception="transcript_only", provenance=provenance)
    response = await messages.process_save_message(None, 1, TestUser(), text_plain="Synthetic request",
        prevalidated=True, runtime_llm_id=10 if authorization == "wrong_backend" else 1,
        expected_llm_id=10, channel_context=context)
    assert response.status_code == (409 if authorization == "wrong_backend" else 403)
    messages.reserve_ai_usage.assert_not_awaited()
    assert not env.provider_calls


@pytest.mark.asyncio
async def test_subscription_live_backend_uses_api_byok_and_bypasses_prompt_engine(live_backend, monkeypatch):
    from integrations.devices import service as devices

    env = live_backend
    with sqlite3.connect(env.path) as db:
        db.execute("UPDATE USER_DETAILS SET api_key_mode='own_only'")
        db.execute("UPDATE PROMPTS SET gransabio_enabled=1")
    monkeypatch.setattr(devices, "load_user_api_keys", AsyncMock(return_value={"openai": "synthetic-owner-key"}))
    seen = []

    async def check_keys(_bound, kwargs):
        seen.append((kwargs["llm_id"], kwargs["model"], kwargs["byok"], kwargs["user_api_key"]))

    scripted_backend(env, monkeypatch, on_call=check_keys)
    await delegate(env)
    assert seen == [(1, "gpt-4.1", True, "synthetic-owner-key")] * 2
    assert_active_result(env, env.tool_results[0][0])


def assert_active_result(env, expected):
    assert len(env.spoken_result) == 1
    encoded, delegation_id = env.spoken_result[0]
    receipt = json.loads(encoded)
    assert receipt["status"] == "live_session_active"
    assert receipt["result"] == expected
    assert delegation_id == "live-delegation"



@pytest.mark.asyncio
@pytest.mark.parametrize("channel,kind", [("web", "gemini_live"), ("device", "openai_live")])
async def test_live_kb_round_trip_uses_database_role_and_returns_second_answer(live_backend, monkeypatch, channel, kind):
    env = live_backend
    scripted_backend(env, monkeypatch)
    session = await delegate(env, channel=channel, kind=kind)
    assert len(env.provider_calls) == 2
    result, count, article = env.tool_results[0]
    assert count == 1 and article == "platform_allowed"
    assert "PLATFORM_ALLOWED" in result
    assert all(marker not in result for marker in ("ADMIN_DENIED", "ALPHA_ALLOWED", "BETA_DENIED", "SHARED_ALLOWED"))
    assert_active_result(env, result)
    assert [tool["name"] for tool in env.provider_calls[1]["tools"]] == ["end_live_session"]
    assert session._pending_end is None
    assert env.provider_calls[1]["web_search_mode"] is None
    session._persist.assert_not_awaited()  # Backend text is not recorded as heard.
    await asyncio.wait_for(env.query_logged.wait(), 2)
    import common
    with sqlite3.connect(env.path) as db:
        row = db.execute("SELECT query_hash,results_count,top_article_id,prompt_id FROM HELP_QUERY_LOG").fetchone()
        assert row == (hmac.new(common.PEPPER.encode(), HELP_QUERY.encode(), hashlib.sha256).hexdigest(), 1, article, 1)
        assert db.execute("SELECT COUNT(*) FROM MESSAGES").fetchone()[0] == 0
        assert db.execute("SELECT llm_id FROM CONVERSATIONS WHERE id=1").fetchone()[0] == 10


@pytest.mark.asyncio
async def test_live_web_search_feeds_verified_result_to_second_backend_response(live_backend, monkeypatch):
    env = live_backend
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, tool="query_perplexity", arguments={"query": WEB_QUERY})
    session = await delegate(env)
    search.assert_awaited_once_with(WEB_QUERY)
    assert env.search_usage.await_args.kwargs["component"]["prompt_id"] == 1
    assert len(env.provider_calls) == 2
    assert_active_result(env, WEB_RESULT)
    assert session._pending_end is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["lookup_platform_help", "query_perplexity"])
@pytest.mark.parametrize("application", [False, True])
async def test_live_lookup_second_pass_executes_close_and_settles_both_calls(
        live_backend, monkeypatch, tool, application):
    env = live_backend
    if application:
        application_scope(env, monkeypatch, allow_search=True)
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, tool=tool, followup_tool="end_live_session",
                     arguments={"query": WEB_QUERY} if tool == "query_perplexity" else None)
    session = await delegate(env, kind="openai_live", end_requested=True)
    assert len(env.provider_calls) == 2
    assert [item["name"] for item in env.provider_calls[1]["tools"]] == ["end_live_session"]
    result = env.tool_results[0][0] if tool == "lookup_platform_help" else WEB_RESULT
    final_message = result + " Goodbye."
    receipt = json.loads(env.spoken_result[0][0])
    assert receipt["status"] == "live_session_end_requested"
    assert receipt["final_message"] == final_message
    assert session._pending_end.final_message == final_message
    assert env.saved == [final_message]
    assert len(env.settlements) == 1
    assert env.settlements[0]["billing_only_accumulated_usage"] is True
    assert env.settlements[0]["billing_reservation_id"] == "synthetic-hold"
    assert [(entry["input_tokens"], entry["output_tokens"]) for entry in env.usage] == [(10, 4), (20, 8)]
    assert all(entry["reservation_id"] == "synthetic-hold" for entry in env.usage)
    assert all(call["billing_reservation_id"] == "synthetic-hold" for call in env.provider_calls)
    messages.extend_ai_reservation.assert_awaited_once()
    messages._settle_or_refund_ai_reservation.assert_awaited_once()
    if tool == "query_perplexity":
        search.assert_awaited_once_with(WEB_QUERY)
    else:
        search.assert_not_awaited()
    session._persist.assert_not_awaited()
    with sqlite3.connect(env.path) as db:
        assert db.execute("SELECT COUNT(*) FROM MESSAGES").fetchone()[0] == 0
        assert db.execute("SELECT llm_id FROM CONVERSATIONS WHERE id=1").fetchone()[0] == 10
    if tool == "lookup_platform_help":
        await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("offered,returned", [(True, "query_perplexity"), (False, "end_live_session")])
async def test_live_lookup_rejects_a_second_tool_that_was_not_offered(
        live_backend, monkeypatch, offered, returned):
    env = live_backend
    if not offered:
        monkeypatch.setattr(messages, "live_tools_for_context", lambda *_args, **_kw: [])
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, followup_tool=returned)
    session = await delegate(env, end_requested=True)
    assert len(env.provider_calls) == 2 and len(env.usage) == 2
    assert env.saved == [] and session._pending_end is None
    if not offered:
        assert env.provider_calls[1]["tools"] is None
    search.assert_not_awaited()
    assert "failed or could not be confirmed" in env.spoken_result[0][0]
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["control", "scope", "user_funding", "application_funding"])
async def test_live_close_revalidates_after_second_provider_returns(live_backend, monkeypatch, revoked):
    env = live_backend
    if revoked in {"scope", "application_funding"}:
        application_scope(env, monkeypatch)

    async def revoke(bound, _kwargs):
        if len(env.provider_calls) != 2:
            return
        if revoked == "control":
            bound.context.provenance["live_session_control"].canceled = True
        elif revoked == "scope":
            from dataclasses import replace
            env.scope = replace(env.scope, context_id="different-workspace")
        elif revoked == "user_funding":
            messages.revalidate_user_billing.return_value = False
        else:
            from integrations.applications import billing
            from integrations.embed.models import EmbedError
            billing.revalidate_application_operation.side_effect = EmbedError("application_funding_required", 403)

    scripted_backend(env, monkeypatch, followup_tool="end_live_session", on_call=revoke)
    session = await delegate(env, end_requested=True)
    assert len(env.provider_calls) == 2 and len(env.usage) == 2
    assert env.saved == [] and session._pending_end is None
    assert "failed or could not be confirmed" in env.spoken_result[0][0]
    messages._settle_or_refund_ai_reservation.assert_awaited_once()
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["lookup_platform_help", "query_perplexity"])
async def test_live_lookup_stops_when_second_call_cannot_be_funded(live_backend, monkeypatch, tool):
    env = live_backend
    monkeypatch.setattr(messages, "extend_ai_reservation", AsyncMock(
        side_effect=messages.InsufficientBalanceError("Synthetic insufficient balance")))
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, tool=tool, followup_tool="end_live_session")
    session = await delegate(env, end_requested=True)
    assert len(env.provider_calls) == 1 and len(env.usage) == 1
    assert env.saved == [] and session._pending_end is None
    search.assert_not_awaited()
    messages._settle_or_refund_ai_reservation.assert_awaited_once()
    if tool == "lookup_platform_help":
        await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [{"terminal": "stopped"}, {"error": "Synthetic provider failure"},
                                   {"persistence_error": True}])
async def test_live_second_pass_terminal_event_prevents_collected_close(live_backend, monkeypatch, event):
    env = live_backend
    scripted_backend(env, monkeypatch, followup_tool="end_live_session", terminal_event=event)
    session = await delegate(env, end_requested=True)
    assert len(env.provider_calls) == 2 and len(env.usage) == 2
    assert env.saved == [] and session._pending_end is None
    assert "failed or could not be confirmed" in env.spoken_result[0][0]
    messages._settle_or_refund_ai_reservation.assert_awaited_once()
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
async def test_live_second_pass_close_requires_successful_persistence(live_backend, monkeypatch):
    env = live_backend
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", AsyncMock(
        side_effect=messages.BillingReservationError("Synthetic settlement failure")))
    scripted_backend(env, monkeypatch, followup_tool="end_live_session")
    session = await delegate(env, end_requested=True)
    assert len(env.provider_calls) == 2 and len(env.usage) == 2
    assert env.saved == [] and session._pending_end is None
    assert "failed or could not be confirmed" in env.spoken_result[0][0]
    messages._settle_or_refund_ai_reservation.assert_awaited_once()
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
async def test_live_application_kb_keeps_own_and_shared_scope(live_backend, monkeypatch):
    env = live_backend
    application_scope(env, monkeypatch)
    scripted_backend(env, monkeypatch)
    await delegate(env)
    assert len(env.provider_calls) == 2
    result, count, article = env.tool_results[0]
    assert count == 2 and article in {"app:alpha/alpha_allowed", "embed/shared_allowed"}
    assert "ALPHA_ALLOWED" in result and "SHARED_ALLOWED" in result
    assert all(marker not in result for marker in ("PLATFORM_ALLOWED", "ADMIN_DENIED", "BETA_DENIED"))
    assert_active_result(env, result)
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["lookup_platform_help", "query_perplexity"])
async def test_live_application_cannot_execute_a_tool_without_its_capability(live_backend, monkeypatch, tool):
    env = live_backend
    application_scope(env, monkeypatch, allow_tools=tool != "lookup_platform_help")
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, tool=tool)
    await delegate(env)
    assert len(env.provider_calls) == 1 and not env.saved and not env.tool_results
    search.assert_not_awaited()
    assert "failed or could not be confirmed" in env.spoken_result[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected", [
    ("no_match", "No help articles matched"),
    ("unavailable", "Help search failed; the documentation could not be checked"),
])
async def test_live_kb_distinguishes_no_match_from_unavailable_database(live_backend, monkeypatch, state, expected):
    env = live_backend
    query = "nonexistentfeature" if state == "no_match" else HELP_QUERY
    if state == "unavailable":
        with sqlite3.connect(env.path) as db:
            db.execute("DROP TABLE HELP_ARTICLES_FTS")
    scripted_backend(env, monkeypatch, arguments={"query": query, "locale": "en"})
    await delegate(env)
    assert len(env.provider_calls) == 2
    result, count, article = env.tool_results[0]
    assert result.startswith(expected) and (count, article) == (0, None)
    assert "does not prove that a feature does not exist" in result
    assert_active_result(env, result)
    await asyncio.wait_for(env.query_logged.wait(), 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty_query", "search_error", "settlement_error"])
async def test_live_web_failure_does_not_return_success_or_retry(live_backend, monkeypatch, failure):
    env = live_backend
    search = scripted_search(env, monkeypatch, fail=failure == "search_error", settled=failure != "settlement_error")
    scripted_backend(env, monkeypatch, tool="query_perplexity", arguments={"query": "" if failure == "empty_query" else WEB_QUERY})
    await delegate(env)
    assert search.await_count == int(failure != "empty_query")
    assert len(env.provider_calls) == 1 and env.saved == []
    assert "failed or could not be confirmed" in env.spoken_result[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled_by", ["prompt", "user"])
async def test_live_rejects_search_returned_by_provider_when_search_is_disabled(live_backend, monkeypatch, disabled_by):
    env = live_backend
    with sqlite3.connect(env.path) as db:
        db.execute("UPDATE PROMPTS SET disable_web_search=1" if disabled_by == "prompt" else
                   "UPDATE USER_DETAILS SET web_search_enabled=0")
    search = scripted_search(env, monkeypatch)
    scripted_backend(env, monkeypatch, tool="query_perplexity", arguments={"query": WEB_QUERY})
    await delegate(env)
    assert all(tool.get("name") != "query_perplexity" for tool in env.provider_calls[0]["tools"])
    search.assert_not_awaited()
    assert len(env.provider_calls) == 1 and env.saved == []


@pytest.mark.asyncio
async def test_probe_acknowledges_receipt_without_retaining_audio_or_transcripts():
    import aiohttp
    from tools import probe_native_live_integration as probe
    events = [
        {"event": "backend", "active": True},
        {"event": "audio", "audio": base64.b64encode(b"\0\1" * 24).decode(), "id": 1, "generation": 0},
        {"event": "transcript", "role": "bot", "text": "SYNTHETIC_PRIVATE_TEXT"},
        {"event": "interrupted"},
        {"event": "audio", "audio": base64.b64encode(b"\0\1" * 24).decode(), "id": 2, "generation": 1},
        {"event": "backend", "active": False},
        {"event": "ending"}, {"event": "ended"},
    ]

    class Socket:
        send_json = AsyncMock()

        async def __aiter__(self):
            for event in events:
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: event)

    state, socket = probe.new_state(), Socket()
    await probe.receive_events(socket, state)
    assert state["ending"] and state["ended"] and state["interruptions"] == 1
    assert state["simulated_acks"] == 2 and state["audio_bytes"] == 96
    assert not state["backend_active"] and state["backend_completed"] == 1
    assert socket.send_json.await_args_list[1].args == ({"event": "played", "id": 2, "generation": 1},)
    assert "SYNTHETIC_PRIVATE_TEXT" not in json.dumps(state)
    assert all("audio" not in call.args[0] and "text" not in call.args[0] for call in socket.send_json.await_args_list)


@pytest.mark.asyncio
async def test_probe_keeps_turn_boundary_while_gpt_streams_silence():
    import aiohttp
    from tools import probe_native_live_integration as probe

    class Socket:
        send_json = AsyncMock()

        async def __aiter__(self):
            event = {"event": "audio", "audio": base64.b64encode(b"\0" * 960).decode(),
                     "id": 1, "generation": 0}
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: event)

    state = probe.new_state()
    state["last_output"] = 123.0
    await probe.receive_events(Socket(), state)
    assert state["last_output"] == 123.0
    assert state["audio_bytes"] == 960 and state["simulated_acks"] == 1


@pytest.mark.asyncio
async def test_probe_waits_for_backend_result_after_a_spoken_preamble(monkeypatch):
    from tools import probe_native_live_integration as probe

    state = probe.new_state()
    reader = asyncio.get_running_loop().create_future()
    clock = [10.0]
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock[0])

    class Socket:
        sends = 0

        async def send_bytes(self, _pcm):
            self.sends += 1
            state["audio_bytes"] += 960
            state["bot_chars"] += 10
            state["last_output"] = 0.0
            state["backend_active"] = self.sends < 3
            if self.sends == 3:
                state["backend_completed"] = 1

    async def advance(_seconds):
        clock[0] += .02

    monkeypatch.setattr(probe.asyncio, "sleep", advance)
    socket = Socket()
    result = await probe.send_stage(socket, reader, state, b"\0" * 640, expect_backend=True)
    assert socket.sends == 3
    assert result["backend_delegations"] == 1
    reader.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("event,code", [
    ({"event": "ended"}, "invalid_ended_order"),
    ({"event": "error", "message": "SYNTHETIC_PRIVATE_KEY"}, "server_reported_error"),
])
async def test_probe_fails_closed_without_echoing_server_details(event, code):
    import aiohttp
    from tools import probe_native_live_integration as probe

    class Socket:
        async def __aiter__(self):
            yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: event)

    with pytest.raises(probe.ProbeError) as exc:
        await probe.receive_events(Socket(), probe.new_state())
    assert str(exc.value) == code


def test_probe_rejects_nonlocal_credentials_destination_and_invalid_media(tmp_path):
    import argparse
    import wave
    from tools import probe_native_live_integration as probe
    for origin in ("https://aurvek.example", "http://user:password@localhost", "http://localhost/other", "http://localhost?token=secret"):
        with pytest.raises(argparse.ArgumentTypeError):
            probe.local_origin(origin)
    assert probe.local_origin("http://127.0.0.1:7789/") == "http://127.0.0.1:7789"
    path = tmp_path / "synthetic.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setparams((2, 2, 16000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0" * 640)
    with pytest.raises(probe.ProbeError, match="invalid_synthetic_wav"):
        probe.synthetic_audio(path)
