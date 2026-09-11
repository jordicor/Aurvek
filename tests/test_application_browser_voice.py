"""Browser audio reaches canonical turns; only played segments are confirmed."""
import asyncio
import base64
import io
import json
import wave
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocket

from ai_runtime.channel_turns import ChannelContext, TurnKey
from integrations.applications import browser_voice as voice
from integrations.applications.runtime import admit_application_turn
from integrations.embed.models import EmbedError
from tests.test_application_runtime import application


def recording(frames=3200):
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * frames)
    return output.getvalue()


def test_voice_pcm_duration_is_derived_and_incomplete_data_rejected():
    audio = recording()
    assert voice.decode_utterance(base64.b64encode(audio).decode()) == (audio, 0.2)
    with pytest.raises(ValueError):
        voice.decode_utterance(base64.b64encode(audio[:-2]).decode())
    with pytest.raises(ValueError):
        voice.decode_utterance(base64.b64encode(recording(16000 * 60 + 1)).decode())


@pytest.mark.asyncio
async def test_live_voice_requires_voice_stt_and_tts_in_same_application():
    scope = application(capabilities={key: True for key in ("text", "voice", "stt", "tts")})
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=scope))
    context = ChannelContext(channel="web", input_origin="web.live_voice",
        input_perception="transcript_only", persistence="deferred", turn_key=TurnKey("browser", "1"))
    assert (await admit_application_turn(None, conversation_id=1, user_id=5,
        channel_context=context, service=service)).application == scope
    service.authorize_runtime_conversation.return_value = application(capabilities={"text": True, "voice": True, "stt": True})
    with pytest.raises(EmbedError, match="application_capability_denied"):
        await admit_application_turn(None, conversation_id=1, user_id=5, channel_context=context, service=service)


@pytest.mark.asyncio
async def test_ws_request_adapter_preserves_origin_csrf_and_empty_form(monkeypatch):
    websocket = WebSocket({"type": "websocket", "scheme": "wss", "path": "/ws/conversations/1/voice",
        "query_string": b"", "server": ("aurvek.example", 443), "client": ("127.0.0.1", 1),
        "headers": [(b"host", b"aurvek.example"), (b"origin", b"https://aurvek.example")],
        "session": {"gptsub_csrf_token": "s" * 40}, "state": {}}, AsyncMock(), AsyncMock())
    request = voice._http_request(websocket, "s" * 40)
    import request_security
    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "")
    from request_security import validate_mutation_request
    assert validate_mutation_request(request) is None
    assert not await request.form()
    assert validate_mutation_request(voice._http_request(websocket, "x" * 40)) is not None


@pytest.fixture
def session(monkeypatch):
    from integrations.applications.web_audio import VoiceRecording
    monkeypatch.setattr(VoiceRecording, "retain_input", AsyncMock())
    monkeypatch.setattr(VoiceRecording, "retain_reply", AsyncMock())
    monkeypatch.setattr(VoiceRecording, "discard_pending", AsyncMock())
    websocket = SimpleNamespace(headers={}, scope={"state": {}}, send_json=AsyncMock())
    session = voice.BrowserVoiceSession(websocket, 1, SimpleNamespace(id=5), object())
    scope = application(capabilities={key: True for key in ("text", "voice", "stt", "tts")})
    monkeypatch.setattr(session, "check_access", AsyncMock(return_value=(scope, 7, None)))
    @asynccontextmanager
    async def operation(_scope, principal):
        yield SimpleNamespace(scope=scope)
    monkeypatch.setattr(voice, "_voice_operation", operation)
    from integrations import media
    monkeypatch.setattr(media, "transcribe_external_audio_detailed", AsyncMock(return_value=SimpleNamespace(text="Transfer me.")))
    monkeypatch.setattr(voice, "voice_configuration", AsyncMock(return_value=object()))
    @asynccontextmanager
    async def connection(**_kwargs):
        yield object()
    monkeypatch.setattr(voice, "get_db_connection", connection)
    return session


def canonical_turn(monkeypatch, text, *, handoff=None):
    from integrations.telephony import transport
    class Runtime:
        def __init__(self):
            self.handle = SimpleNamespace(committed=False)
            self.confirmed = None
            self.interrupted = None
            self.handoff_result = None
            self.handoff_failure = None
        async def events_until_draft(self):
            yield SimpleNamespace(content=text, persistence_error=False)
        async def confirm_audible(self, prefix, *, played_ms):
            self.confirmed = (prefix, played_ms)
            self.handle.committed = True
            self.handoff_result = handoff
        async def interrupt(self, prefix, *, played_ms, reason):
            self.interrupted = (prefix, played_ms)
            self.handle.committed = True
    runtime = Runtime()
    monkeypatch.setattr(transport, "start_canonical_voice_turn", AsyncMock(return_value=runtime))
    return runtime


@pytest.mark.asyncio
async def test_voice_handoff_follows_confirmation_and_keeps_session(session, monkeypatch):
    text = "I will connect you with the other assistant."
    runtime = canonical_turn(monkeypatch, text, handoff={"conversation_id": 2, "assistant_id": "other"})
    async def played(segment, _voice):
        assert runtime.confirmed is None
        assert session.conversation_id == 1
        return 1200
    monkeypatch.setattr(session, "_speak", played)
    original_session = session.session_id
    await session._run_turn((recording(), .2))
    from integrations.telephony import transport
    context = transport.start_canonical_voice_turn.await_args.kwargs["channel_context"]
    assert context.on_commit_in_transaction is not None
    assert runtime.confirmed == (text, 1200)
    assert session.conversation_id == 2 and session.generation == 1
    assert session.session_id == original_session
    events = [call.args[0]["event"] for call in session.websocket.send_json.call_args_list]
    assert events[-2:] == ["handoff", "ready"]


@pytest.mark.asyncio
async def test_barge_in_keeps_only_previously_played_segments(session, monkeypatch):
    first = "This sentence was completely played. "
    runtime = canonical_turn(monkeypatch, first + "This next sentence was interrupted.")
    async def played(segment, _voice):
        if segment == first:
            return 1100
        session.interrupted.set()
        return None
    monkeypatch.setattr(session, "_speak", played)
    await session._run_turn((recording(), .2))
    assert runtime.interrupted == (first, 1100)
    assert runtime.confirmed is None
    assert session.conversation_id == 1


@pytest.mark.asyncio
async def test_audio_is_not_confirmed_merely_when_sent(session, monkeypatch, tmp_path):
    from tools import tts
    from integrations.applications.web_audio import VoiceRecording
    session.recording = VoiceRecording(session.conversation_id, session.user.id)
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"audio")
    monkeypatch.setattr(tts, "handle_tts_request", AsyncMock(return_value=(str(path), None)))
    sent = asyncio.Event()
    async def send(event, **_data):
        if event == "audio":
            sent.set()
    monkeypatch.setattr(session, "send", send)
    pending = asyncio.create_task(session._speak("Hello.", object()))
    await asyncio.wait_for(sent.wait(), 1)
    assert not pending.done()
    assert not session.recording.played_segments
    session.ack.set_result(450)
    assert await pending == 450
    assert session.recording.played_segments == [b"audio"]


@pytest.mark.asyncio
async def test_hosted_voice_does_not_bypass_application_runtime(monkeypatch):
    from integrations.elevenlabs import service
    from integrations.applications import runtime
    monkeypatch.setattr(runtime, "authorize_application_read", AsyncMock(return_value=application()))
    with pytest.raises(ValueError, match="shared voice"):
        await service._reject_application_hosted_voice(None, 1, 5)


@pytest.mark.asyncio
async def test_transfer_rechecks_input_after_destination_preparation(session, monkeypatch):
    prepared = voice.PreparedBrowserHandoff(session, object(), (7, "elevenlabs", "same-voice"))
    async def prepare(*_args):
        session.interrupted.set()
        return prepared.configuration
    monkeypatch.setattr(session, "_prepare_destination", prepare)
    with pytest.raises(EmbedError, match="handoff_source_interrupted"):
        await prepared.commit(None, application(), application(conversation_id=2))


@pytest.mark.asyncio
async def test_socket_cancellation_settles_before_releasing_funding(session, monkeypatch):
    held = []
    @asynccontextmanager
    async def operation(scope, principal):
        held.append(True)
        try:
            yield SimpleNamespace(scope=scope)
        finally:
            held.clear()
    monkeypatch.setattr(voice, "_voice_operation", operation)
    runtime = canonical_turn(monkeypatch, "This answer has not played yet.")
    interrupt = runtime.interrupt
    async def settle(*args, **kwargs):
        assert held
        return await interrupt(*args, **kwargs)
    runtime.interrupt = settle
    speaking = asyncio.Event()
    async def speak(*_args):
        speaking.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(session, "_speak", speak)
    session.turn_task = asyncio.create_task(session._run_turn((recording(), .2)))
    await asyncio.wait_for(speaking.wait(), 1)
    session.closed = True
    session._interrupt()
    with pytest.raises(asyncio.CancelledError):
        await session.turn_task
    assert runtime.interrupted == ("", 0)
    assert not held


def test_embed_voice_socket_requires_frame_and_checks_csrf_before_start(monkeypatch):
    from dataclasses import replace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from integrations.embed.middleware import EmbedHostMiddleware
    from integrations.embed.models import EMBED_COOKIE_NAME
    from tests.test_embed_route_security import ScopedStore, FRAME_A, ORIGIN, COOKIE, CSRF
    from marketplace.middleware import custom_domains
    monkeypatch.setattr(custom_domains, "_primary_domains", {"aurvek.example"})
    store = ScopedStore()
    principal = store.frames[FRAME_A]
    scope = application(user_id=principal.user_id, conversation_id=principal.conversation_id)
    store.frames[FRAME_A] = replace(principal, capabilities={"text": True, "voice": True}, application=scope)
    monkeypatch.setattr(voice, "get_embed_user", AsyncMock(return_value=SimpleNamespace(id=1)))
    monkeypatch.setattr(voice.BrowserVoiceSession, "check_access", AsyncMock(return_value=(scope, 7, principal)))
    monkeypatch.setattr(voice, "voice_configuration", AsyncMock())
    @asynccontextmanager
    async def connection(**_kwargs):
        yield object()
    monkeypatch.setattr(voice, "get_db_connection", connection)
    entered = []
    async def run(session):
        entered.append(session.conversation_id)
        await session.send("ready")
    monkeypatch.setattr(voice.BrowserVoiceSession, "run", run)
    app = FastAPI()
    app.include_router(voice.router)
    app.add_middleware(EmbedHostMiddleware, store=store)
    with TestClient(app, base_url=ORIGIN) as client:
        client.cookies.set(EMBED_COOKIE_NAME, COOKIE)
        path = ORIGIN.replace("https:", "wss:") + f"/ws/conversations/101/voice?embed_frame={FRAME_A}"
        with client.websocket_connect(path, headers={"Origin": ORIGIN}) as websocket:
            websocket.send_json({"csrf_token": CSRF})
            assert websocket.receive_json()["event"] == "ready"
        with client.websocket_connect(path, headers={"Origin": ORIGIN}) as websocket:
            websocket.send_json({"csrf_token": "x" * 48})
            assert websocket.receive_json()["event"] == "error"
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(path.replace("/101/", "/102/")):
                pass
    assert entered == [101]
