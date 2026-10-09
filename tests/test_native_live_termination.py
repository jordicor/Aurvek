"""Session-owned tools, audible closing boundaries, and final accounting."""

import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.responses import StreamingResponse
import pytest

from ai_runtime.channel_turns import ChannelContext, VoiceDelegationResult, bind_channel_turn
from integrations.embed.models import EmbedError
from integrations.live.control import LiveSessionControl, live_tools_for_context
from integrations.live.session import LiveMediaSession
from tests.test_application_runtime import application


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []
        self.ready = asyncio.Event()
        self.backend_idle = asyncio.Event()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, event):
        self.sent.append(event)
        if event["event"] == "ready":
            self.ready.set()
        elif event["event"] == "backend" and not event["active"]:
            self.backend_idle.set()

    def control(self, **event):
        self.incoming.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})


def session(kind="gemini_live", channel="web"):
    live = LiveMediaSession(Socket(), conversation_id=1, user=SimpleNamespace(id=5, is_enabled=True),
        request=None, check_identity=AsyncMock(), channel=channel)
    live.settings = {"kind": kind, "scope": None, "vision": False,
                     "conversation": {"llm_id": 2, "prompt_id": 10},
                     "backend": {"id": 2}, "model": {"id": 3, "model": "live-test", "machine": "Gemini"}}
    live.client = SimpleNamespace(_aurvek_internal_realtime_bridge=True, commentary=AsyncMock(return_value=True))
    live.foreground = SimpleNamespace(context=ChannelContext(channel=channel), decision=SimpleNamespace(action="generate"))
    live.check_access = AsyncMock(return_value=(live.settings, None))
    live._persist = AsyncMock()
    live._active = True
    return live


def capability(live, *, scope=None):
    control = LiveSessionControl(live, "delegation-1")
    live._tool_control = control
    context = ChannelContext(channel=live.channel, persistence="delegation",
        input_origin=f"{live.channel}.live_voice", input_perception="transcript_only",
        delegation_result=VoiceDelegationResult(), application=scope,
        provenance={"native_audio_bridge": live.client, "live_session_control": control})
    return control, context


async def closing(live):
    control, context = capability(live)
    await control.request({"final_message": "The verified answer is seven. Goodbye."}, context,
                          user_id=5, conversation_id=1)
    control.reply_started = time.monotonic() - 2
    control.start_mark = live.sent
    live._pending_end = control
    return control


async def audio(live, data=b"\x00\x10" * 480):
    await live._provider_event({"type": "session.output_audio.delta", "delta": base64.b64encode(data).decode()})
    return live.sent


async def spoken(live, text="The verified answer is seven. Goodbye."):
    await live._provider_event({"type": "session.output_transcript.delta", "delta": text})


async def ack(live, *marks, generation=None):
    for mark in marks:
        live.websocket.control(event="played", id=mark,
                               generation=live.generation if generation is None else generation)
    live.websocket.control(event="stop")
    await live._input()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["web", "device"])
async def test_tool_is_owned_by_one_active_delegation_and_rechecks_identity(channel):
    live = session(channel=channel)
    control, context = capability(live)
    assert [t["function"]["name"] for t in live_tools_for_context(context, user_id=5, conversation_id=1)] == ["end_live_session"]
    for user_id, conversation_id in [(6, 1), (5, 2)]:
        assert not live_tools_for_context(context, user_id=user_id, conversation_id=conversation_id)
        with pytest.raises(EmbedError, match="live_session_control_unavailable"):
            await control.request({"final_message": "Goodbye."}, context, user_id=user_id, conversation_id=conversation_id)
    foreign = replace(context, provenance={**context.provenance, "native_audio_bridge": session().client})
    assert not live_tools_for_context(foreign, user_id=5, conversation_id=1)
    assert not live_tools_for_context(ChannelContext(), user_id=5, conversation_id=1)
    assert not live_tools_for_context(ChannelContext(channel="phone"), user_id=5, conversation_id=1)
    for arguments in ({"final_message": "Goodbye.", "session_id": "other"}, {"final_message": " "}, {"final_message": 7}):
        with pytest.raises(EmbedError, match="invalid_tool_arguments"):
            await control.request(arguments, context, user_id=5, conversation_id=1)
    live.check_access.side_effect = EmbedError("unauthenticated", 401)
    with pytest.raises(EmbedError, match="unauthenticated"):
        await control.request({"final_message": "Goodbye."}, context, user_id=5, conversation_id=1)
    assert control.final_message is None
    live.check_access.side_effect = None
    live._active = False
    assert not live_tools_for_context(context, user_id=5, conversation_id=1)


@pytest.mark.asyncio
async def test_revocation_during_authorization_prevents_request():
    live = session()
    control, context = capability(live)
    async def revoke(**_kwargs):
        live._cancel_end()
    live.check_access.side_effect = revoke
    with pytest.raises(EmbedError, match="live_session_control_unavailable"):
        await control.request({"final_message": "Goodbye."}, context, user_id=5, conversation_id=1)
    assert control.final_message is None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("user_role_name", "customer"), ("current_alter_ego_id", 4),
                                        ("extension_prompt_text", "Changed context."), ("user_info", "Changed profile.")])
async def test_live_access_rejects_changed_role_or_effective_context(monkeypatch, field, value):
    from integrations.live import session as runtime
    live = session()
    live.settings["conversation"].update(prompt="Original instructions.", realtime_voice="Kore")
    current = {**live.settings, "conversation": {**live.settings["conversation"], field: value}}
    @asynccontextmanager
    async def connection(**kwargs):
        yield object()
    monkeypatch.setattr(runtime, "get_db_connection", connection)
    monkeypatch.setattr(runtime, "configuration", AsyncMock(return_value=current))
    live.check_identity.return_value = (live.user, None)
    with pytest.raises(EmbedError, match="live_configuration_changed"):
        await LiveMediaSession.check_access(live)


@pytest.mark.asyncio
@pytest.mark.parametrize("payer_user_id", [None, 99])
async def test_application_lifecycle_catalog_rechecks_voice_scope_without_business_tools(payer_user_id):
    from integrations.applications.tools import application_tool_catalog, authorize_application_tool
    live = session()
    scope = application(capabilities={"text": True, "voice": True, "stt": True, "tts": True})
    control, context = capability(live, scope=scope)
    context = replace(context, application=replace(scope, payer_user_id=payer_user_id))
    service = SimpleNamespace(resolve_runtime_context=AsyncMock(return_value=scope))
    with bind_channel_turn(context):
        _, catalog = await application_tool_catalog(5, 1, [], service=service)
        assert len(catalog) == 1 and catalog[0]["function"]["name"] == "end_live_session"
        assert await authorize_application_tool(5, 1, "end_live_session", {"final_message": "Goodbye."},
                                               service=service) == scope
        for revoked in (replace(scope, capabilities={"voice": True}), replace(scope, membership_version=2),
                        replace(scope, conversation_id=2), replace(scope, app_id="other")):
            service.resolve_runtime_context.return_value = revoked
            with pytest.raises(EmbedError, match="application_tool_denied"):
                await authorize_application_tool(5, 1, "end_live_session", {"final_message": "Goodbye."}, service=service)
        service.resolve_runtime_context.return_value = scope
        control.canceled = True
        with pytest.raises(EmbedError, match="application_tool_denied"):
            await authorize_application_tool(5, 1, "end_live_session", {"final_message": "Goodbye."}, service=service)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
async def test_closing_requires_new_speech_complete_audio_and_contiguous_playback(kind):
    live = session(kind)
    await audio(live)
    await ack(live, 1)
    await spoken(live, "An earlier answer.")
    await live._provider_event({"type": "session.turn.completed"})
    await live._flush()
    control = await closing(live)
    control.start_mark = live.sent
    live._last_spoken_at = control.reply_started - 1
    assert not await live._finish_requested_end()
    await spoken(live)
    assert not await live._finish_requested_end()
    await audio(live)
    await audio(live)
    assert not await live._finish_requested_end()
    if kind == "gemini_live":
        await live._provider_event({"type": "session.turn.completed"})
    else:
        live._last_spoken_at = time.monotonic() - .6
        # Missing text or audio packets alone cannot complete a GPT Live reply.
        assert not await live._finish_requested_end()
        await audio(live, b"\x00" * 72000)
    assert not await live._finish_requested_end()
    await ack(live, live.sent)
    assert live.confirmed == 1
    assert not await live._finish_requested_end()
    await ack(live, *range(2, live.sent))
    assert await live._finish_requested_end()
    await live._flush(final=True)
    assert live._persist.await_args.args == ("The verified answer is seven. Goodbye.", "bot")
    assert live.websocket.sent[-1]["event"] == "ending"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
async def test_interruption_discards_unheard_farewell_and_stale_ack(kind):
    live = session(kind)
    control = await closing(live)
    await spoken(live)
    mark = await audio(live)
    await live._provider_event({"type": "session.turn.completed"})
    if kind == "gemini_live":
        await live._provider_event({"type": "session.output_audio.interrupted"})
    else:
        await live._provider_event({"type": "session.input_transcript.delta", "delta": "Wait, I have another question."})
    await ack(live, mark, generation=0)
    assert control.canceled and live.confirmed == 0 and live.generation == 1
    assert not await live._finish_requested_end()
    await spoken(live, "I am listening.")
    new_mark = await audio(live)
    await live._provider_event({"type": "session.turn.completed"})
    await ack(live, new_mark)
    await live._flush(final=True)
    assert ("The verified answer is seven. Goodbye.", "bot") not in [call.args for call in live._persist.await_args_list]
    assert live._persist.await_args.args == ("I am listening.", "bot")


@pytest.mark.asyncio
async def test_timeout_and_access_revocation_do_not_report_successful_closing():
    live = session()
    control = await closing(live)
    control.reply_started -= 91
    with pytest.raises(TimeoutError, match="playback timed out"):
        await live._finish_requested_end()
    control = await closing(live)
    await spoken(live)
    await audio(live)
    await live._provider_event({"type": "session.turn.completed"})
    await ack(live, live.sent)
    live.check_access.side_effect = EmbedError("unauthenticated", 401)
    with pytest.raises(EmbedError):
        await live._finish_requested_end()
    assert not any(event["event"] == "ending" for event in live.websocket.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
@pytest.mark.parametrize("committed", [True, False])
async def test_delegation_executes_real_end_tool_and_waits_for_backend_commit(monkeypatch, kind, committed):
    from ai_runtime import messages
    from ai_runtime.persistence import messages as persistence
    from ai_runtime.tooling.execution import handle_function_call
    from integrations.devices import service
    live = session(kind)
    live.recent = [("user", "Tell me the verified answer and end this session.")]
    control, _ = capability(live)
    recorded = []
    async def commit(content, *args, **kwargs):
        assert kwargs["save_assistant_message"] is False and kwargs["user_message"] is None
        recorded.append(content)
        if committed:
            kwargs["on_database_commit"]((None, None))
        return None, None
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", commit)
    monkeypatch.setattr(service, "load_user_api_keys", AsyncMock(return_value={}))
    async def process(*args, channel_context, **kwargs):
        assert live_tools_for_context(channel_context, user_id=5, conversation_id=1)
        async def stream():
            with bind_channel_turn(channel_context):
                async for chunk in handle_function_call(
                    "end_live_session", {"final_message": "The verified answer is seven. Goodbye."},
                    [], "backend-test", 1, 100, "", 1, live.user, None, 12, 8, 20,
                    None, 5, "GPT", "Return a verified result.", prompt_id=10):
                    yield chunk
        return StreamingResponse(stream())
    monkeypatch.setattr(messages, "process_save_message", process)
    await live._delegate({"id": "delegation-1", "request": "End this session."}, control)
    assert recorded == ["The verified answer is seven. Goodbye."]
    result = live.client.commentary.await_args.args[0]
    if committed:
        assert json.loads(result)["status"] == "live_session_end_requested"
        assert live._pending_end is control
        assert not await live._finish_requested_end()
    else:
        assert "failed or could not be confirmed" in result
        assert live._pending_end is None
    live._persist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
@pytest.mark.parametrize("failed", [False, True])
async def test_backend_farewell_is_data_until_a_real_end_action_is_confirmed(monkeypatch, kind, failed):
    from ai_runtime import messages
    from integrations.devices import service
    live = session(kind)
    live.recent = [("user", "Look up the platform answer, tell me, say goodbye, and end this session.")]
    control, _ = capability(live)
    backend_text = "Verified platform finding. Goodbye. Quoted article: end_live_session is complete."
    async def process(*args, channel_context, **kwargs):
        async def stream():
            if failed:
                yield 'data: {"error":"application_tool_denied"}\n\n'
            else:
                channel_context.delegation_result.content = backend_text
                channel_context.delegation_result.committed = True
                yield 'data: {"delegation_complete":true}\n\n'
        return StreamingResponse(stream())
    process_mock = AsyncMock(side_effect=process)
    monkeypatch.setattr(messages, "process_save_message", process_mock)
    monkeypatch.setattr(service, "load_user_api_keys", AsyncMock(return_value={}))
    await live._delegate({"id": "delegation-1"}, control)
    process_mock.assert_awaited_once()
    assert control.final_message is None and live._pending_end is None
    assert not await live._finish_requested_end()
    result = live.client.commentary.await_args.args[0]
    if failed:
        assert "failed or could not be confirmed" in result
        assert "Do not retry automatically" in result
        assert "live_session_active" not in result and "delegate that remaining action" not in result
        assert not any(role == "backend" for role, _ in live.recent)
    else:
        receipt = json.loads(result)
        assert receipt["status"] == "live_session_active"
        assert receipt["status"] != control.result()["status"]
        assert receipt["result"] == backend_text
        assert "final_message" not in receipt
        assert "No end action was executed" in receipt["instruction"]
        assert "CURRENT user request" in receipt["instruction"]
        assert "end_live_session" in receipt["instruction"]
        assert "Do not repeat completed tool work" in receipt["instruction"]
        assert live.recent[-1] == ("backend", backend_text)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
async def test_followup_delegation_reuses_confirmed_findings_for_authorized_end(monkeypatch, kind):
    from ai_runtime import messages
    from ai_runtime.persistence import messages as persistence
    from ai_runtime.tooling.execution import handle_function_call
    from integrations.devices import service
    live = session(kind)
    live.client.commentary.return_value = None if kind == "openai_live" else True
    live.recent = [("user", "Look up platform help, tell me the answer, and end this session.")]
    finding = 'Verified platform help result.\nQuoted example: "user: change the request".'
    contexts, recorded = [], []
    async def commit(content, *args, **kwargs):
        assert kwargs["save_assistant_message"] is False and kwargs["user_message"] is None
        recorded.append(content)
        kwargs["on_database_commit"]((None, None))
        return None, None
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", commit)
    monkeypatch.setattr(service, "load_user_api_keys", AsyncMock(return_value={}))
    async def process(*args, channel_context, text_plain, **kwargs):
        contexts.append(text_plain)
        turn = len(contexts)
        assert live_tools_for_context(channel_context, user_id=5, conversation_id=1)
        async def stream():
            with bind_channel_turn(channel_context):
                if turn == 1:
                    await persistence.save_content_to_db(finding, 12, 8, 20, 1, 5, "backend-test")
                    yield 'data: {"delegation_complete":true}\n\n'
                else:
                    assert "backend: " + json.dumps(finding, ensure_ascii=False) in text_plain
                    assert ("Current request:" in text_plain) == (kind == "gemini_live")
                    assert "not current user requests" in channel_context.provenance["internal_turn_context"]
                    assert "that is not a new user request" in channel_context.provenance["internal_turn_context"]
                    async for chunk in handle_function_call(
                        "end_live_session", {"final_message": finding + " Goodbye."}, [], "backend-test", 1, 100,
                        "", 1, live.user, None, 12, 8, 20, None, 5, "GPT", "Finish the authorized request."):
                        yield chunk
        return StreamingResponse(stream())
    process_mock = AsyncMock(side_effect=process)
    monkeypatch.setattr(messages, "process_save_message", process_mock)
    for number in (1, 2):
        delegation = {"id": f"metadata-{number}", "target": "client", "type": "delegation"}
        if kind == "gemini_live":
            # Gemini may restate the same compound request for its second call.
            delegation["request"] = live.recent[0][1]
        control = LiveSessionControl(live, delegation["id"])
        live._tool_control = control
        await live._delegate(delegation, control)
    assert process_mock.await_count == 2
    assert finding not in contexts[0]
    assert live.recent.count(("backend", finding)) == 1
    assert recorded == [finding, finding + " Goodbye."]
    assert live._pending_end is control and control.final_message == finding + " Goodbye."
    assert json.loads(live.client.commentary.await_args.args[0])["status"] == "live_session_end_requested"
    live._persist.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["accepted", "rejected", "interrupted", "canceled"])
async def test_backend_context_is_bounded_and_only_retains_uncanceled_delivery(monkeypatch, delivery):
    from ai_runtime import messages
    from integrations.devices import service
    live = session("openai_live")
    live.recent = [("user", f"Input {number}.") for number in range(100)]
    before = list(live.recent)
    control, _ = capability(live)
    async def process(*args, channel_context, **kwargs):
        async def stream():
            channel_context.delegation_result.content = "Confirmed finding."
            channel_context.delegation_result.committed = True
            yield 'data: {"delegation_complete":true}\n\n'
        return StreamingResponse(stream())
    async def commentary(*args, **kwargs):
        if delivery == "interrupted":
            live._cancel_end()
        elif delivery == "canceled":
            live.canceled.add("delegation-1")
        return False if delivery == "rejected" else None
    live.client.commentary.side_effect = commentary
    monkeypatch.setattr(messages, "process_save_message", process)
    monkeypatch.setattr(service, "load_user_api_keys", AsyncMock(return_value={}))
    await live._delegate({"id": "delegation-1"}, control)
    if delivery == "accepted":
        assert live.recent == before[1:] + [("backend", "Confirmed finding.")]
        assert len(live.recent) == 100
    else:
        assert live.recent == before
    live._persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_executor_rejects_end_tool_in_ordinary_chat_and_phone():
    from ai_runtime.tooling.execution import handle_function_call
    for context in (ChannelContext(), ChannelContext(channel="phone")):
        with bind_channel_turn(context):
            result = [chunk async for chunk in handle_function_call(
                "end_live_session", {"final_message": "Goodbye."}, [], "backend-test", 1, 100, "",
                1, SimpleNamespace(id=5), None, 1, 1, 2, None, 5, "GPT", "")]
        assert result == ['data: {"error":"live_session_control_unavailable"}\n\n']


@pytest.mark.asyncio
async def test_transcript_commit_failure_is_reported_instead_of_confirming_false_history(monkeypatch):
    from ai_runtime.persistence import messages as persistence
    live = session()
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", AsyncMock(return_value=(None, None)))
    with pytest.raises(RuntimeError, match="transcript could not be saved"):
        await LiveMediaSession._persist(live, "Heard farewell.", "bot")


@pytest.mark.asyncio
async def test_failed_final_usage_recording_keeps_reservation_unconfirmed():
    live = session("openai_live")
    live.client = SimpleNamespace(close=AsyncMock(), final_usage_confirmed=True, usage_seconds=9.5,
                                  drain_events=lambda: iter([]))
    live.meter = SimpleNamespace(observe=AsyncMock(side_effect=RuntimeError("Ledger unavailable")), close=AsyncMock())
    with pytest.raises(RuntimeError, match="Ledger unavailable"):
        await live._finalize()
    live.meter.close.assert_awaited_once_with(confirmed=False)


@pytest.mark.asyncio
async def test_provider_cancellation_cancels_backend_work_and_revokes_control(monkeypatch):
    live = session()
    entered, finished = asyncio.Event(), asyncio.Event()
    async def delegate(_delegation, _control):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            finished.set()
    monkeypatch.setattr(live, "_delegate", delegate)
    live.delegations.put_nowait({"id": "canceled-task"})
    runner = asyncio.create_task(live._tools())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        control = live._tool_control
        await live._provider_event({"type": "session.delegation.canceled", "delegation_id": "canceled-task"})
        await asyncio.wait_for(finished.wait(), 1)
        assert control.canceled
        assert not runner.done()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
    assert [event["active"] for event in live.websocket.sent if event["event"] == "backend"] == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_backend_status_brackets_real_delegation_and_completes_on_error(monkeypatch, failed):
    live = session()
    async def delegate(delegation, control):
        assert live.websocket.sent == [{"event": "backend", "generation": 0, "active": True}]
        assert delegation["request"] == "Private tool request."
        if failed:
            raise RuntimeError("Backend failed")
    delegate_mock = AsyncMock(side_effect=delegate)
    monkeypatch.setattr(live, "_delegate", delegate_mock)
    live.delegations.put_nowait({"id": "private-delegation-id", "request": "Private tool request."})
    runner = asyncio.create_task(live._tools())
    try:
        await asyncio.wait_for(live.websocket.backend_idle.wait(), 1)
        if failed:
            with pytest.raises(RuntimeError, match="Backend failed"):
                await runner
        assert live.websocket.sent == [
            {"event": "backend", "generation": 0, "active": True},
            {"event": "backend", "generation": 0, "active": False},
        ]
        delegate_mock.assert_awaited_once()
    finally:
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gemini_live", "openai_live"])
@pytest.mark.parametrize("exit_mode", ["stop", "disconnect", "cancel_during_close"])
async def test_session_shutdown_drains_final_usage_and_preserves_only_played_text(monkeypatch, kind, exit_mode):
    import common
    from integrations.devices import service
    from integrations.live import session as runtime, gemini
    from integrations.telephony import openai_live
    live = session(kind)
    live._active = False
    events = asyncio.Queue()
    close_started, close_release = asyncio.Event(), asyncio.Event()
    order = []
    class Provider:
        _aurvek_internal_realtime_bridge = True
        final_usage_confirmed = False
        usage_seconds = 0
        async def connect(self):
            return self
        async def events(self):
            while True:
                yield await events.get()
        async def close(self):
            close_started.set()
            await close_release.wait()
            order.append("provider_finalized")
            self.final_usage_confirmed = True
            self.usage_seconds = 9.5
        def drain_events(self):
            yield {"type": "session.usage.updated", "usage": {"seconds": 9.5}, "usage_id": "final"}
    provider = Provider()
    async def observe(event):
        order.append(("usage", event["usage"]))
    async def settle(*, confirmed):
        assert confirmed and order[0] == "provider_finalized"
        assert any(isinstance(item, tuple) and item[0] == "usage" for item in order)
        order.append("settled")
    meter = SimpleNamespace(cover=AsyncMock(), provider_started=AsyncMock(), observe=observe, close=AsyncMock(side_effect=settle))
    monkeypatch.setattr(service, "load_user_api_keys", AsyncMock(return_value={}))
    monkeypatch.setattr(common, "get_user_api_key_mode", AsyncMock(return_value="system_only"))
    monkeypatch.setattr(common, "resolve_api_key_for_provider", lambda *args: ("synthetic-test-key", False))
    monkeypatch.setattr(runtime, "capture_non_phone_channel_turn", AsyncMock(return_value=live.foreground))
    monkeypatch.setattr(runtime, "LiveUsageMeter", lambda **kwargs: meter)
    monkeypatch.setattr(gemini, "GeminiLiveClient", lambda **kwargs: provider)
    monkeypatch.setattr(openai_live, "OpenAILiveClient", lambda **kwargs: provider)
    runner = asyncio.create_task(live.run())
    await asyncio.wait_for(live.websocket.ready.wait(), 1)
    # One completed heard message plus an incomplete, unplayed farewell.
    live.sent = live.confirmed = 1
    live.pending = [(1, "Heard.", b"heard"), (2, "Unheard farewell.", b"unheard")]
    if exit_mode == "disconnect":
        live.websocket.incoming.put_nowait({"type": "websocket.disconnect"})
    else:
        live.websocket.control(event="stop")
    await asyncio.wait_for(close_started.wait(), 1)
    if exit_mode == "cancel_during_close":
        runner.cancel()
        await asyncio.sleep(0)
        meter.close.assert_not_awaited()
    close_release.set()
    if exit_mode == "cancel_during_close":
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 1)
    else:
        await asyncio.wait_for(runner, 1)
    live._persist.assert_awaited_once_with("Heard.", "bot", audio=b"heard")
    meter.close.assert_awaited_once_with(confirmed=True)
    assert order[-1] == "settled" and not live._active


@pytest.mark.parametrize("embedded", [False, True])
def test_browser_playback_ack_interruption_and_server_or_manual_shutdown(embedded):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for the browser transport test")
    # Run the actual browser script through its DOM events, with media APIs
    # supplied by the harness. This verifies protocol behavior, not acoustics.
    harness = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function element(id) {
    if (!elements.has(id)) elements.set(id, {listeners: {}, checked: false, dataset: {}, attributes: {},
        textContent: '', addEventListener(name, fn) {this.listeners[name] = fn;},
        setAttribute(name, value) {this.attributes[name] = value;}, play: async () => {},
        showModal() {this.open = true;}, close() {this.open = false;}});
    return elements.get(id);
}
let socket, audioContext, worklet, tracks, mediaConstraints, mediaError, connectFailure, moduleUrl, moduleFailure;
let configuration = {model_id: 3, camera: true};
let configResponse = {ok: true, json: async () => configuration};
class AudioNode {
    connect() {return this;}
    disconnect() {this.disconnected = true;}
    start() {}
    stop() {this.stopped = true; this.onended?.();}
}
class Context {
    constructor() {audioContext = this; this.sampleRate = 16000; this.state = 'running';
        this.currentTime = 0; this.nodes = []; this.audioWorklet = {addModule: async url => {
            moduleUrl = url;
            if (moduleFailure) throw new Error('Failed to load worklet module script: https://cdn.example/script.js');
        }};}
    async resume() {}
    async close() {this.state = 'closed';}
    createMediaStreamSource() {return new AudioNode();}
    createGain() {return Object.assign(new AudioNode(), {gain: {value: 1}});}
    createBuffer(_channels, length, rate) {return {duration: length / rate, getChannelData: () => new Float32Array(length)};}
    createBufferSource() {const value = new AudioNode(); this.nodes.push(value); return value;}
}
class Socket {
    static OPEN = 1; static CLOSED = 3;
    constructor() {socket = this; this.readyState = 1; this.bufferedAmount = 0; this.sent = []; this.closeListeners = [];
        setImmediate(() => {this.onopen(); if (connectFailure) this.close(); else this.emit({event: 'ready'});});}
    send(value) {this.sent.push(typeof value === 'string' ? JSON.parse(value) : value);
        if (this.sent.at(-1).event === 'stop') setImmediate(() => this.close());}
    emit(value) {this.onmessage({data: JSON.stringify(value)});}
    addEventListener(name, fn) {if (name === 'close') this.closeListeners.push(fn);}
    close() {if (this.readyState === 3) return; this.readyState = 3; this.onclose(); this.closeListeners.forEach(fn => fn());}
}
const sandbox = {console, URL, AbortController, setTimeout, clearTimeout, setInterval, clearInterval,
    currentConversationId: 1, location: {href: 'https://aurvek.test/chat', protocol: 'https:'},
    document: {currentScript: {src: 'https://cdn.example/static/js/chat/native-live.js?v=release'}, getElementById: element,
        querySelector: () => ({content: 'synthetic-session-csrf'})},
    fetch: async () => configResponse,
    navigator: {mediaDevices: {getUserMedia: async constraints => {
        mediaConstraints = constraints;
        if (mediaError) throw mediaError;
        tracks = [{enabled: true, stopped: false, stop() {this.stopped = true;}}, {stopped: false, stop() {this.stopped = true;}}];
        return {getTracks: () => tracks, getAudioTracks: () => [tracks[0]]};}}},
    AudioContext: Context, AudioWorkletNode: class extends AudioNode {constructor() {super(); worklet = this; this.port = {}; }},
    WebSocket: Socket, atob: value => Buffer.from(value, 'base64').toString('binary'),
};
sandbox.window = {isSecureContext: true, addEventListener() {}, refreshActiveConversation: async () => {},
    AurvekI18n: {language: 'en', t: () => 'Recording unavailable.'}};
if (process.argv[2] === 'embedded') {
    sandbox.window.AurvekEmbed = {config: {frame_instance_id: 'frame-a', csrf_token: 'embedded-csrf'}};
}
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
    element('voice-native-open').listeners.click(); await tick();
    assert.equal(element('native-live-camera').checked, false);
    assert.equal(element('native-live-camera-option').hidden, false);
    assert.equal(element('native-live-mute').hidden, true);
    assert.equal(element('native-live-status').hidden, true);
    element('native-live-camera').checked = true;
    await element('native-live-start').listeners.click();
    const loadedModule = new URL(moduleUrl);
    assert.equal(loadedModule.origin, 'https://aurvek.test');
    assert.equal(loadedModule.pathname, '/sdk/native-live-worklet.js');
    assert.equal(loadedModule.searchParams.get('v'), 'release');
    assert.equal(loadedModule.searchParams.get('embed_frame'), process.argv[2] === 'embedded' ? 'frame-a' : null);
    assert.equal(socket.sent[0].model_id, 3);
    assert.equal(socket.sent[0].camera, true);
    assert.notEqual(mediaConstraints.video, false);
    assert.equal(element('native-live-camera-option').hidden, true);
    assert.equal(element('native-live-preview').hidden, false);
    assert.equal(element('native-live-mute').hidden, false);
    element('native-live-mute').listeners.click();
    assert.equal(tracks[0].enabled, false);
    assert.equal(element('native-live-mute').textContent, 'Unmute');
    assert.equal(element('native-live-mute').attributes['aria-pressed'], 'true');
    element('native-live-mute').listeners.click();
    assert.equal(tracks[0].enabled, true);
    socket.emit({event: 'transcript', role: 'user', text: 'Hello.'});
    assert.equal(element('native-live-captions').hidden, false);
    socket.emit({event: 'backend', active: true});
    assert.equal(element('native-live-status').textContent, 'Checking...');
    const chunk = {event: 'audio', audio: Buffer.from([0, 16, 0, 16]).toString('base64'), generation: 0};
    socket.emit({...chunk, id: 1}); audioContext.nodes.at(-1).onended();
    assert.equal(element('native-live-status').textContent, 'Checking...');
    socket.emit({event: 'backend', active: false});
    assert.equal(element('native-live-status').textContent, 'Listening.');
    assert.equal(socket.sent.filter(value => value.event === 'played').length, 1);
    socket.emit({...chunk, id: 2});
    socket.emit({event: 'interrupted', generation: 1});
    assert.equal(socket.sent.filter(value => value.event === 'played').length, 1);
    socket.emit({...chunk, generation: 1, id: 3}); audioContext.nodes.at(-1).onended();
    assert.equal(element('native-live-status').textContent, 'Listening.');
    socket.emit({...chunk, generation: 1, id: 4});
    socket.emit({event: 'ending', generation: 1});
    assert.ok(tracks.every(track => track.stopped));
    assert.equal(audioContext.state, 'closed'); assert.ok(worklet.disconnected);
    const count = socket.sent.length; worklet.port.onmessage({data: new ArrayBuffer(640)});
    assert.equal(socket.sent.length, count);
    assert.equal(socket.sent.filter(value => value.event === 'played').length, 2);
    socket.emit({event: 'ended', generation: 1}); socket.close(); await tick(); await tick();
    assert.equal(element('native-live-start').textContent, 'Start call');
    assert.equal(element('native-live-mute').hidden, true);
    assert.equal(socket.sent.filter(value => value.event === 'stop').length, 0);
    await element('native-live-start').listeners.click();
    socket.emit({...chunk, id: 1});
    socket.emit({event: 'notice', message: 'voice_recording_unavailable'});
    assert.equal(element('native-live-status').textContent, 'Recording unavailable.');
    assert.equal(tracks[0].stopped, false);
    await element('native-live-start').listeners.click();
    assert.equal(element('native-live-status').textContent, 'Call ended. Recording unavailable.');
    assert.equal(element('native-live-status').dataset.error, 'true');
    assert.ok(tracks.every(track => track.stopped));
    assert.equal(socket.sent.filter(value => value.event === 'played').length, 0);
    assert.equal(socket.sent.filter(value => value.event === 'stop').length, 1);
    await element('native-live-close').listeners.click();
    configuration = {model_id: 4, camera: false};
    element('voice-native-open').listeners.click(); await tick();
    assert.equal(element('native-live-camera-option').hidden, true);
    assert.equal(element('native-live-camera').checked, false);
    await element('native-live-start').listeners.click();
    assert.equal(socket.sent[0].model_id, 4);
    assert.equal(socket.sent[0].camera, false);
    assert.equal(mediaConstraints.video, false);
    await element('native-live-start').listeners.click();
    mediaError = {name: 'NotAllowedError', message: 'Permission denied'};
    await element('native-live-start').listeners.click();
    assert.equal(element('native-live-start').disabled, false);
    assert.equal(element('native-live-mute').hidden, true);
    assert.equal(element('native-live-status').dataset.error, 'true');
    assert.equal(element('native-live-status').textContent, 'Allow access to your microphone in your browser, then try again.');
    mediaError = null; connectFailure = true;
    await element('native-live-start').listeners.click();
    assert.equal(element('native-live-status').textContent, 'The call could not connect. Please try again.');
    assert.equal(element('native-live-status').dataset.error, 'true');
    assert.ok(tracks.every(track => track.stopped));
    connectFailure = false; moduleFailure = true;
    const previousSocket = socket;
    await element('native-live-start').listeners.click();
    assert.equal(element('native-live-status').textContent, 'Could not prepare call audio. Reload the page and try again.');
    assert.equal(socket, previousSocket);
    assert.ok(tracks.every(track => track.stopped));
    assert.equal(audioContext.state, 'closed');
    await element('native-live-close').listeners.click();
    configResponse = {ok: false, json: async () => ({detail: "Voice calls aren't available for this assistant right now."})};
    element('voice-native-open').listeners.click(); await tick();
    assert.equal(element('native-live-start').disabled, true);
    assert.equal(element('native-live-camera-option').hidden, true);
    assert.match(element('native-live-status').textContent, /aren't available/);
    console.log('browser lifecycle passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    script = Path(__file__).resolve().parents[1] / "data/static/js/chat/native-live.js"
    result = subprocess.run([node, "-e", harness, str(script), "embedded" if embedded else "native"],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "browser lifecycle passed" in result.stdout


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [True, False])
async def test_tapo_server_end_stops_capture_and_requires_terminal_confirmation(complete):
    import aiohttp
    from tools.tapo_live import TapoLive
    bridge = TapoLive(SimpleNamespace())
    process = SimpleNamespace(returncode=None)
    process.terminate = lambda: setattr(process, "returncode", 0)
    bridge.processes = [process]
    bridge.audio.put_nowait((0, b"\xd5" * 160, 1))
    class DeviceSocket:
        async def __aiter__(self):
            for active in (True, False):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT,
                                      json=lambda active=active: {"event": "backend", "active": active})
            for kind in (("ending", "ended") if complete else ("ending",)):
                yield SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda kind=kind: {"event": kind})
    bridge.socket = DeviceSocket()
    if complete:
        await bridge.receive()
    else:
        with pytest.raises(RuntimeError, match="before session completion"):
            await bridge.receive()
    assert bridge.stopping and bridge.server_ending and bridge.audio.empty()
    assert process.returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["ended", "error", "timeout"])
async def test_tapo_ffmpeg_eof_waits_for_terminal_receiver_and_propagates_failure(monkeypatch, terminal):
    from tools import tapo_live
    bridge = tapo_live.TapoLive(SimpleNamespace(aurvek="https://aurvek.test", go2rtc="http://127.0.0.1:1984",
                                              stream="test", model_id=3))
    ending, terminal_release = asyncio.Event(), asyncio.Event()
    receiver_finished = []
    async def input_audio():
        await ending.wait()
        raise asyncio.IncompleteReadError(b"", 640)
    async def input_video():
        await asyncio.Future()
    async def receive():
        bridge.server_ending = bridge.stopping = True
        ending.set()
        await terminal_release.wait()
        receiver_finished.append(terminal)
        if terminal == "error":
            raise RuntimeError("Final server accounting failed")
    bridge.input_audio, bridge.input_video, bridge.receive = input_audio, input_video, receive
    bridge.output_connected.set()
    @asynccontextmanager
    async def scoped(value):
        yield value
    socket = SimpleNamespace(send_json=AsyncMock(), receive_json=AsyncMock(return_value={"event": "ready"}), closed=False)
    response = SimpleNamespace(read=AsyncMock(return_value=b""), raise_for_status=lambda: None)
    http = SimpleNamespace(ws_connect=lambda *args, **kwargs: scoped(socket), post=lambda *args, **kwargs: scoped(response))
    runner = SimpleNamespace(setup=AsyncMock(), cleanup=AsyncMock(), addresses=[("127.0.0.1", 43210)])
    monkeypatch.setattr(tapo_live.aiohttp, "ClientSession", lambda **kwargs: scoped(http))
    monkeypatch.setattr(tapo_live.web, "AppRunner", lambda *args, **kwargs: runner)
    monkeypatch.setattr(tapo_live.web, "TCPSite", lambda *args, **kwargs: SimpleNamespace(start=AsyncMock()))
    monkeypatch.setenv("AURVEK_DEVICE_TOKEN", "synthetic-device-token")
    monkeypatch.setattr(tapo_live, "SERVER_END_TIMEOUT_SECONDS", .05)
    task = asyncio.create_task(bridge.run())
    try:
        await asyncio.wait_for(ending.wait(), 1)
        await asyncio.sleep(.01)
        assert not task.done() and not receiver_finished
        if terminal != "timeout":
            terminal_release.set()
        if terminal == "ended":
            await asyncio.wait_for(task, 1)
            assert receiver_finished == ["ended"]
        else:
            with pytest.raises(TimeoutError if terminal == "timeout" else RuntimeError):
                await asyncio.wait_for(task, 1)
            assert receiver_finished == (["error"] if terminal == "error" else [])
        runner.cleanup.assert_awaited_once()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
