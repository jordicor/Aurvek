"""Native media admission, playback provenance, and reservation boundaries."""

import asyncio
import base64
import io
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from ai_runtime.channel_turns import ChannelContext, VoiceDelegationResult
from integrations.applications.runtime import admit_application_turn
from integrations.embed.models import EmbedError
from integrations.live import routes
from integrations.live.billing import LiveUsageMeter
from integrations.live.session import LiveMediaSession, decode_frame
from integrations.telephony.realtime_bridge import RealtimeUsage
from tests.test_application_runtime import application


def test_worklet_route_serves_javascript_without_a_cdn_redirect():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get("/sdk/native-live-worklet.js?v=release", follow_redirects=False)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "location" not in response.headers
    assert "registerProcessor('aurvek-native-microphone'" in response.text


@pytest.mark.parametrize("origin", ["web.live_voice", "device.live_voice", "phone.live_call"])
def test_fresh_schema_accepts_native_audio_and_rejects_false_provenance(origin):
    with sqlite3.connect(":memory:") as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript((Path(__file__).parents[1] / "aurvek_schema.sql").read_text(encoding="utf-8"))
        conn.execute("INSERT INTO USERS(id,username) VALUES(1,'Synthetic user')")
        conn.execute("INSERT INTO CONVERSATIONS(id,user_id) VALUES(1,1)")
        conn.execute("INSERT INTO MESSAGES(id,conversation_id,user_id,message,type) VALUES(1,1,1,'Synthetic speech','user')")
        conn.execute("INSERT INTO MESSAGE_INPUT_PROVENANCE(message_id,origin,perception) VALUES(1,?,'audio_native')", (origin,))
        assert conn.execute("SELECT origin,perception FROM MESSAGE_INPUT_PROVENANCE").fetchone() == (origin, "audio_native")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE MESSAGE_INPUT_PROVENANCE SET origin='web.message'")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_camera_frames_have_strict_size_and_format_limits():
    blob = io.BytesIO()
    Image.new("RGB", (640, 480), "blue").save(blob, "JPEG")
    assert decode_frame(base64.b64encode(blob.getvalue()).decode()) == blob.getvalue()
    with pytest.raises(ValueError):
        decode_frame("x" * 400000)
    blob = io.BytesIO()
    Image.new("RGB", (1400, 20)).save(blob, "JPEG")
    with pytest.raises(ValueError):
        decode_frame(base64.b64encode(blob.getvalue()).decode())


@pytest.mark.asyncio
async def test_camera_jitter_drops_early_frames_without_stopping_audio_or_later_video(monkeypatch):
    from integrations.live import session as runtime

    blob = io.BytesIO()
    Image.new("RGB", (16, 16), "blue").save(blob, "JPEG")
    frame = {"type": "websocket.receive", "text": json.dumps({
        "event": "frame", "jpeg": base64.b64encode(blob.getvalue()).decode(),
    })}
    audio = b"\x00\x00" * 320
    incoming = iter([
        (10.0, frame),
        (11.156, frame),
        (12.102, frame),  # 946 ms, as observed during the production lookup.
        (12.120, {"type": "websocket.receive", "bytes": audio}),
        (13.202, frame),
        (13.220, {"type": "websocket.receive", "text": '{"event":"stop"}'}),
    ])
    now = 10.0

    async def receive():
        nonlocal now
        now, message = next(incoming)
        return message

    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=lambda: now))
    session = LiveMediaSession(SimpleNamespace(receive=receive, send_json=AsyncMock()),
        conversation_id=1, user=SimpleNamespace(id=5), request=None,
        check_identity=AsyncMock(), camera=True)
    session.settings = {"kind": "gemini_live", "vision": True}
    session.client = SimpleNamespace(send_audio=AsyncMock(), send_video=AsyncMock())
    session.check_access = AsyncMock()

    await session._input()

    assert session.client.send_video.await_count == 3
    assert all(call.args == (blob.getvalue(),) for call in session.client.send_video.await_args_list)
    session.client.send_audio.assert_awaited_once_with(audio)
    assert session.check_access.await_count == 3
    session.websocket.send_json.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["web", "device"])
async def test_application_delegation_keeps_capability_gate_and_truthful_text_perception(channel):
    bridge = SimpleNamespace(_aurvek_internal_realtime_bridge=True)
    context = ChannelContext(channel=channel, input_origin=f"{channel}.live_voice",
        input_perception="transcript_only", persistence="delegation", delegation_result=VoiceDelegationResult(),
        provenance={"native_audio_bridge": bridge})
    scope = application(capabilities={key: True for key in ("text", "voice", "stt", "tts")})
    service = SimpleNamespace(authorize_runtime_conversation=AsyncMock(return_value=scope))
    assert (await admit_application_turn(None, conversation_id=1, user_id=5, channel_context=context, service=service)).application == scope
    service.authorize_runtime_conversation.return_value = application(capabilities={"text": True, "voice": True})
    with pytest.raises(EmbedError):
        await admit_application_turn(None, conversation_id=1, user_id=5, channel_context=context, service=service)
    with pytest.raises(ValueError, match="trusted native"):
        ChannelContext(channel=channel, persistence="delegation", delegation_result=VoiceDelegationResult())


@pytest.mark.asyncio
async def test_only_acknowledged_output_is_persisted_and_new_input_is_not_lost_during_commit():
    session = LiveMediaSession(SimpleNamespace(send_json=AsyncMock()), conversation_id=1,
        user=SimpleNamespace(id=5), request=None, check_identity=AsyncMock())
    session.settings = {"kind": "gemini_live"}
    session.foreground = object()
    session.check_access = AsyncMock()
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []
    saved_audio = []
    async def persist(text, role, *, audio):
        saved.append((text, role))
        saved_audio.append(bytes(audio))
        if text == "first":
            entered.set()
            await release.wait()
    session._persist = persist
    session.fragments["user"] = ["first"]
    session.audio["user"].extend(b"first-pcm")
    flushing = asyncio.create_task(session._flush())
    await entered.wait()
    session.fragments["user"].append("second")
    session.audio["user"].extend(b"second-pcm")
    release.set()
    await flushing
    assert session.fragments["user"] == ["second"]
    session.fragments["bot"] = ["unheard answer"]
    session.sent, session.confirmed = 4, 3
    await session._provider_event({"type": "session.turn.completed"})
    await session._flush()
    assert ("unheard answer", "bot") not in saved
    session.confirmed = 4
    await session._flush()
    assert saved == [("first", "user"), ("second", "user"), ("unheard answer", "bot")]
    assert saved_audio == [b"first-pcm", b"second-pcm", b""]


@pytest.mark.asyncio
async def test_native_billing_deduplicates_usage_and_preserves_unknown_usage(monkeypatch):
    from integrations.live import billing
    for name in ("extend_ai_reservation", "mark_ai_reservation_provider_started",
                 "accumulate_ai_reservation_usage", "refund_fixed_usage", "settle_accumulated_ai_reservation_usage"):
        monkeypatch.setattr(billing.ledger, name, AsyncMock())
    monkeypatch.setattr(billing.ledger, "estimate_customer_charge_from_api_cost", AsyncMock(return_value=.75))
    monkeypatch.setattr(billing.ledger, "reserve_ai_usage", AsyncMock(return_value="reservation"))
    meter = LiveUsageMeter(user_id=5, prompt_id=7, kind="gemini_live")
    await meter.cover()
    await meter.provider_started()
    event = {"usage_id": "turn1", "usage": RealtimeUsage(input_tokens=100, text_input_tokens=100)}
    await meter.observe(event)
    await meter.observe(event)
    assert billing.ledger.accumulate_ai_reservation_usage.await_count == 1
    await meter.close(confirmed=False)
    billing.ledger.refund_fixed_usage.assert_not_awaited()
    billing.ledger.settle_accumulated_ai_reservation_usage.assert_not_awaited()
    await meter.close(confirmed=True)
    billing.ledger.settle_accumulated_ai_reservation_usage.assert_awaited_once()


@pytest.mark.asyncio
async def test_hello_rejects_invalid_camera_or_model_before_provider_dispatch():
    for values in ({"event": "start", "model_id": True}, {"event": "start", "camera": "yes"}):
        socket = SimpleNamespace(receive_text=AsyncMock(return_value=json.dumps(values)))
        with pytest.raises(ValueError):
            await routes._hello(socket)


def test_tapo_bridge_keeps_credentials_and_media_on_the_local_camera_network():
    from tools.tapo_live import aurvek_url, local_url
    assert aurvek_url("https://aurvek.example") == "wss://aurvek.example/ws/devices/live"
    assert local_url("rtsp://127.0.0.1:8554/tapo", scheme={"rtsp"}).endswith("/tapo")
    for url in ("http://public.example", "https://user:password@aurvek.example"):
        with pytest.raises(ValueError):
            aurvek_url(url)
    with pytest.raises(ValueError):
        local_url("http://0.0.0.0:1984", scheme={"http"})


@pytest.mark.asyncio
@pytest.mark.parametrize("origin,token,allowed", [
    ("https://aurvek.example", "s" * 40, True),
    ("https://other.example", "s" * 40, False),
    ("https://aurvek.example", "x" * 40, False),
])
async def test_native_browser_checks_origin_and_session_csrf_before_dispatch(monkeypatch, origin, token, allowed):
    from starlette.websockets import WebSocket
    import auth
    import common
    import request_security
    monkeypatch.setattr(common, "READONLY_MODE", False)
    monkeypatch.setattr(request_security, "PRIMARY_APP_DOMAIN", "aurvek.example")
    monkeypatch.setattr(auth, "get_current_user_from_websocket", AsyncMock(return_value=SimpleNamespace(id=5, is_enabled=True)))
    serve = AsyncMock()
    monkeypatch.setattr(routes, "_serve", serve)
    receive = AsyncMock(side_effect=[{"type": "websocket.connect"}, {"type": "websocket.receive",
        "text": json.dumps({"event": "start", "csrf_token": token})}])
    send = AsyncMock()
    socket = WebSocket({"type": "websocket", "scheme": "wss", "path": "/ws/conversations/1/live",
        "query_string": b"", "server": ("aurvek.example", 443), "client": ("127.0.0.1", 1),
        "headers": [(b"host", b"aurvek.example"), (b"origin", origin.encode())],
        "session": {"gptsub_csrf_token": "s" * 40}, "state": {}}, receive, send)
    await routes.browser_live(socket, 1)
    assert serve.await_count == int(allowed)


@pytest.mark.asyncio
async def test_device_binding_and_capabilities_are_revalidated_during_native_session(monkeypatch):
    from integrations.devices import service
    import auth
    import common
    monkeypatch.setattr(common, "READONLY_MODE", False)
    device = SimpleNamespace(id=3, user_id=5, capabilities={"listen": True, "speak": True, "snapshot": True})
    monkeypatch.setattr(service, "authenticate_device_token", AsyncMock(return_value=device))
    binding = AsyncMock(return_value=SimpleNamespace(status="bound", conversation_id=7))
    monkeypatch.setattr(service, "resolve_runtime_binding", binding)
    monkeypatch.setattr(auth, "get_user_by_id", AsyncMock(return_value=SimpleNamespace(id=5, is_enabled=True)))
    async def serve(socket, **kwargs):
        assert kwargs["conversation_id"] == 7  # Never trust a destination supplied by the device.
        assert kwargs["channel"] == "device"
        identity = kwargs["check_identity"]
        device.capabilities["snapshot"] = False
        with pytest.raises(EmbedError, match="device_capability_denied"):
            await identity()
        device.capabilities["snapshot"] = True
        binding.return_value = SimpleNamespace(status="bound", conversation_id=8)
        with pytest.raises(EmbedError, match="device_binding_changed"):
            await identity()
    monkeypatch.setattr(routes, "_serve", serve)
    socket = SimpleNamespace(headers={"authorization": "Bearer avd_test"}, accept=AsyncMock(),
        close=AsyncMock(), send_json=AsyncMock(), receive_text=AsyncMock(return_value=json.dumps({
            "event": "start", "camera": True, "conversation_id": 999})))
    await routes.device_live(socket)
    socket.send_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_interrupt_discards_unheard_reply_and_close_keeps_completed_heard_tail():
    session = LiveMediaSession(SimpleNamespace(send_json=AsyncMock()), conversation_id=1,
        user=SimpleNamespace(id=5), request=None, check_identity=AsyncMock())
    session.settings, session.foreground = {"kind": "gemini_live"}, object()
    session.check_access = AsyncMock()
    session._persist = AsyncMock()
    session.sent, session.confirmed = 3, 2
    session.pending = [(2, "Heard.", b"heard"), (3, "Not heard.", b"unheard")]
    session.fragments["bot"] = ["Incomplete."]
    await session._provider_event({"type": "session.output_audio.interrupted"})
    await session._flush()
    session._persist.assert_awaited_once_with("Heard.", "bot", audio=b"heard")
    assert session.generation == 1 and not session.fragments["bot"]
    session.sent = session.confirmed = 4
    session.fragments["bot"] = ["Final played answer."]
    await session._provider_event({"type": "session.turn.completed"}, draining=True)
    await session._flush(final=True)
    assert session._persist.await_args.args == ("Final played answer.", "bot")
