from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import httpx
import json
from pathlib import Path
import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator
from xml.etree import ElementTree

import common
from integrations.telephony.billing import PhoneBillingError, PhoneBillingExhausted
from integrations.telephony.elevenlabs_realtime import ElevenLabsRealtimeClient
from integrations.telephony.repository import (
    PhoneHangupAttemptClaim,
    TelephonyConflictError,
    TelephonyInboundUnavailableError,
    TelephonyNotFoundError,
    TelephonyStateError,
)
from integrations.telephony.realtime_call import OpenAIRealtimeCallBridge
from integrations.telephony.routes import (
    MEDIA_STREAM_PATH,
    TelephonyProviderRuntime,
    _build_private_call_audio_token,
    _build_private_audio_token,
    _parse_private_call_audio_token,
    _parse_private_audio_token,
    _peek_start,
    create_telephony_provider_router,
)
from integrations.telephony.schemas import PhoneCallStatus
from integrations.telephony.session import (
    PhoneMediaSessionContext,
    PhoneMediaSessionResult,
)


AUTH_TOKEN = "test-auth-token"
ACCOUNT_SID = "AC" + "1" * 32
CALL_SID = "CA" + "2" * 32
TOKEN = "a" * 43


class FakeRepository:
    def __init__(self, call=None):
        self.call = call
        self.inbound = []
        self.statuses = []
        self.hangup_requests = []
        self.reconnect_callbacks = set()
        self.reconnect_transitions = 0

    async def create_inbound_call(self, **values):
        self.inbound.append(values)
        if self.call is None:
            raise TelephonyNotFoundError("unknown")
        return self.call, True

    async def reconcile_outbound_twiml(self, **values):
        return self.call

    async def record_call_status(
        self,
        event,
        *,
        dispatch_token=None,
        expected_direction=None,
    ):
        self.statuses.append((event, dispatch_token))
        if (
            self.call is not None
            and expected_direction is not None
            and self.call.get("direction") != expected_direction
        ):
            return None, False
        return self.call, True

    async def record_recording_status(self, event, *, dispatch_token=None):
        if self.call is None or dispatch_token != TOKEN:
            return None, False
        return self.call, True

    async def get_call_by_dispatch_token(self, token):
        if self.call is None or token != TOKEN:
            return None
        return self.call

    async def get_stream_attempt_result(self, *, call_id, stream_attempt):
        if (
            self.call is None
            or str(self.call["id"]) != str(call_id)
        ):
            return None
        return {
            "stream_attempt": int(stream_attempt),
            "reason": "websocket_closed",
            "reconnectable": int(stream_attempt) < 2,
            "internal_failure": True,
        }

    async def prepare_reconnect(self, **values):
        # Let route tests exercise the authoritative CAS-loser reread path.
        await asyncio.sleep(0)
        dedupe_key = str(values.get("dedupe_key") or "")
        if dedupe_key in self.reconnect_callbacks:
            return self.call
        if (
            self.call is None
            or int(self.call.get("reconnect_count", -1)) >= 2
            or int(self.call.get("reconnect_count", -1))
            != int(values["stream_attempt"])
        ):
            return None
        self.reconnect_callbacks.add(dedupe_key)
        self.call["reconnect_count"] += 1
        self.reconnect_transitions += 1
        return self.call

    async def record_hangup_requested(self, **values):
        self.hangup_requests.append(values)
        return PhoneHangupAttemptClaim(
            call_id=str(values["call_id"]),
            provider_call_sid=str(values["provider_call_sid"]),
            state="in_flight",
            attempt_count=1,
            attempt_token="connect-action-attempt",
            lease_until="2030-01-01T00:01:00Z",
            reason=str(values["reason"]),
            target_status=values["target_status"].value,
            origin=str(values["origin"]),
            claimed=True,
        )

    async def get_hangup_attempt(self, *, call_id, provider_call_sid):
        if (
            self.call is None
            or str(self.call["id"]) != str(call_id)
            or str(self.call["provider_call_sid"]) != str(provider_call_sid)
            or not self.hangup_requests
        ):
            return None
        request = self.hangup_requests[-1]
        return {
            "state": "in_flight",
            "reason": request["reason"],
            "target_status": request["target_status"].value,
            "origin": request["origin"],
        }


class FakeConnectBillingGate:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls = []

    async def prepare(self, **values):
        self.calls.append(values)
        if self.error is not None:
            raise self.error
        return ()


class FakeBillingService:
    def __init__(self):
        self.duration_calls = []
        self.amd_calls = []
        self.duration_errors = []
        self.amd_errors = []
        self.rate_calls = []
        self.available_stt_providers = {"elevenlabs", "openai"}

    async def rate_available(self, **values):
        self.rate_calls.append(values)
        return values.get("provider") in self.available_stt_providers

    async def reconcile_signed_twilio_duration(self, **values):
        self.duration_calls.append(values)
        if self.duration_errors:
            raise self.duration_errors.pop(0)
        return {}

    async def reconcile_signed_twilio_amd(self, **values):
        self.amd_calls.append(values)
        if self.amd_errors:
            raise self.amd_errors.pop(0)
        return object()


async def ready():
    return True


async def context_ready(_context):
    return True


async def greeting_loader(_context):
    return object()


async def notice_loader(_context, _kind):
    return object()


async def unknown_notice_loader():
    class Asset:
        cache_id = 42
        mp3_path = Path(__file__)

        @staticmethod
        def read_pcmu():
            return b"\xff" * 160

    return Asset()


async def inbound_unavailable_notice_loader():
    class Asset:
        cache_id = 43
        mp3_path = Path(__file__)

        @staticmethod
        def read_pcmu():
            return b"\xfe" * 160

    return Asset()


async def call_notice_asset_loader(_context, notice_key):
    class Asset:
        cache_id = 84
        audio_revision = 9
        technical_notice_key = notice_key

        @staticmethod
        def read_pcmu():
            return b"\xff" * 160

    return Asset()


class FakeVoiceClient:
    async def end_call_once(self, _call_sid):
        return True


class SequencedVoiceClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    async def end_call_once(self, call_sid):
        self.calls.append(call_sid)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class DurableHangupRepository(FakeRepository):
    def __init__(self, call):
        super().__init__(call)
        self.actions = []
        self.attempts = 0

    async def get_call_by_dispatch_token(self, token):
        assert token == TOKEN
        return self.call

    async def append_provider_event(self, **values):
        self.actions.append(("event", values["event_type"]))
        return True

    async def record_hangup_requested(self, **values):
        self.attempts += 1
        self.actions.append(("requested", values["reason"]))
        return PhoneHangupAttemptClaim(
            call_id=str(self.call["id"]),
            provider_call_sid=CALL_SID,
            state="in_flight",
            attempt_count=self.attempts,
            attempt_token=f"attempt-token-{self.attempts}",
            lease_until="2030-01-01T00:01:00Z",
            reason=str(values["reason"]),
            target_status=values["target_status"].value,
            origin=str(values["origin"]),
            claimed=True,
        )

    async def mark_hangup_unresolved(self, **values):
        self.actions.append(("unresolved", values["reason"]))
        return True

    async def mark_hangup_accepted(self, **_values):
        self.actions.append(("accepted", "machine"))
        return True


def call_row():
    return {
        "id": "call-1",
        "dispatch_token": TOKEN,
        "reconnect_count": 0,
        "provider_call_sid": CALL_SID,
        "status": "in_progress",
        "owner_user_id": 1,
        "conversation_id": 10,
        "foreground_fencing_token": 7,
        "foreground_lease_owner": "media:call-1",
        "config_snapshot_json": json.dumps(
            {
                "prompt_id": 2,
                "llm_id": 3,
                "audio_revision": 9,
                "stt_locale": "multi",
                "max_duration_seconds": 3600,
                "warning_milestones_seconds": [900, 300, 180, 60],
                "silence_prompt_seconds": None,
                "silence_hangup_seconds": None,
                "ai_initiation_mode": "on_request",
                "inbound_greeting_mode": "inherit",
                "outbound_greeting_mode": "inherit",
                "recording_default": False,
                "amd_default": False,
            }
        ),
        "recording_enabled": 0,
        "direction": "inbound",
        "created_at": "2026-08-31T00:00:00Z",
    }


def app_for(repository, **overrides):
    values = dict(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        notice_loader=notice_loader,
        greeting_loader=greeting_loader,
        context_readiness=context_ready,
        unknown_notice_loader=unknown_notice_loader,
        inbound_unavailable_notice_loader=inbound_unavailable_notice_loader,
        call_notice_asset_loader=call_notice_asset_loader,
        voice_client=FakeVoiceClient(),
        billing_service=FakeBillingService(),
        connect_billing_gate=FakeConnectBillingGate(),
    )
    values.update(overrides)
    runtime = TelephonyProviderRuntime(**values)
    app = FastAPI()
    app.include_router(create_telephony_provider_router(runtime))
    return app


def test_runtime_builds_lazy_scribe_session_from_snapshot() -> None:
    resolutions = []

    def key_provider():
        resolutions.append("resolved")
        return "elevenlabs-key"

    repository = FakeRepository(call_row())
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        repository=repository,
        readiness_check=ready,
        elevenlabs_api_key_provider=key_provider,
    )
    call = call_row()
    snapshot_values = json.loads(call["config_snapshot_json"])
    snapshot_values["endpointing_ms"] = 1_250
    snapshot_values["stt_locale"] = "es-ES"
    call["config_snapshot_json"] = json.dumps(snapshot_values)
    context = PhoneMediaSessionContext.from_call(
        call,
        account_sid=ACCOUNT_SID,
        stream_attempt=0,
    )

    session = runtime.build_session(context)

    assert isinstance(session.stt, ElevenLabsRealtimeClient)
    assert session.stt.options.endpointing_ms == 1_250
    assert session.stt.options.language_code == "es"
    assert resolutions == []


def test_runtime_builds_openai_realtime_session_without_scribe() -> None:
    repository = FakeRepository(call_row())
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        repository=repository,
        readiness_check=ready,
        elevenlabs_api_key_provider=None,
        openai_api_key_provider=lambda: "openai-key",
    )
    call = call_row()
    snapshot = json.loads(call["config_snapshot_json"])
    snapshot.update(
        runtime_kind="openai_realtime",
        runtime_model="gpt-realtime-2.1-mini",
        phone_realtime_voice="marin",
    )
    call["config_snapshot_json"] = json.dumps(snapshot)
    context = PhoneMediaSessionContext.from_call(
        call,
        account_sid=ACCOUNT_SID,
        stream_attempt=0,
    )

    session = runtime.build_session(context)

    assert isinstance(session.stt, OpenAIRealtimeCallBridge)
    assert session.stt.options.model == "gpt-realtime-2.1-mini"
    assert session.stt.options.voice == "marin"
    assert session._billing_meter.stt_provider == "openai"


@pytest.mark.asyncio
async def test_call_readiness_selects_credentials_and_stt_rate_by_runtime() -> None:
    billing = FakeBillingService()
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        repository=FakeRepository(call_row()),
        readiness_check=ready,
        elevenlabs_api_key_provider=None,
        openai_api_key_provider=lambda: "openai-key",
        context_readiness=context_ready,
        billing_service=billing,
    )
    standard = call_row()
    realtime = call_row()
    snapshot = json.loads(realtime["config_snapshot_json"])
    snapshot.update(
        runtime_kind="openai_realtime",
        runtime_model="gpt-realtime-2.1-mini",
        phone_realtime_voice="marin",
    )
    realtime["config_snapshot_json"] = json.dumps(snapshot)

    assert not await runtime.call_ready(standard)
    assert await runtime.call_ready(realtime)
    assert [call["provider"] for call in billing.rate_calls] == ["openai"]

    runtime.elevenlabs_api_key_provider = lambda: "elevenlabs-key"
    assert await runtime.call_ready(standard)
    assert [call["provider"] for call in billing.rate_calls] == [
        "openai",
        "elevenlabs",
    ]


def signed_headers(path: str, form: dict[str, str]):
    url = f"https://aurvek.example{path}"
    signature = RequestValidator(AUTH_TOKEN).compute_signature(url, form)
    return {"X-Twilio-Signature": signature}


@pytest.fixture(autouse=True)
def canonical_domain(monkeypatch):
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "aurvek.example")


@pytest.mark.asyncio
async def test_invalid_http_signature_is_rejected_fail_closed() -> None:
    repository = FakeRepository(call_row())
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            "/webhooks/twilio/voice/inbound",
            data={"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"},
            headers={"X-Twilio-Signature": "invalid"},
        )

    assert response.status_code == 403
    assert repository.inbound == []


@pytest.mark.parametrize(
    ("reason", "reconnectable", "internal_failure"),
    (
        pytest.param("twilio_stop", False, False, id="end-call-stop"),
        pytest.param(
            "websocket_closed", True, True, id="retryable-websocket-disconnect"
        ),
    ),
)
def test_media_stream_does_not_republish_authoritative_session_outcome(
    reason,
    reconnectable,
    internal_failure,
) -> None:
    class Repository(FakeRepository):
        def __init__(self):
            super().__init__(call_row())
            self.attempt_result_writes = []

        async def record_stream_attempt_result(self, **values):
            self.attempt_result_writes.append(values)
            raise TelephonyConflictError("Stream attempt result changed")

    class Session:
        async def run(self, websocket, *, initial_messages):
            assert len(tuple(initial_messages)) == 2
            # PhoneMediaSession already persisted the authoritative outcome,
            # whether an end_call Stop or a retryable transport disconnect.
            await websocket.close(code=1000)
            return PhoneMediaSessionResult(
                reason=reason,
                stream_sid="MZ" + "3" * 32,
                caller_turns=1,
                reconnectable=reconnectable,
                internal_failure=internal_failure,
                attempt_result_published=True,
            )

    repository = Repository()
    app = app_for(repository, session_factory=lambda _context: Session())
    signature = RequestValidator(AUTH_TOKEN).compute_signature(
        "wss://aurvek.example/ws/twilio/media-stream",
        {},
    )
    connected = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    start = {
        "event": "start",
        "sequenceNumber": "1",
        "streamSid": "MZ" + "3" * 32,
        "start": {
            "accountSid": ACCOUNT_SID,
            "callSid": CALL_SID,
            "streamSid": "MZ" + "3" * 32,
            "tracks": ["inbound"],
            "mediaFormat": {
                "encoding": "audio/x-mulaw",
                "sampleRate": 8000,
                "channels": 1,
            },
            "customParameters": {
                "correlation_token": TOKEN,
                "stream_attempt": "0",
            },
        },
    }

    with TestClient(app, base_url="https://aurvek.example") as client:
        with client.websocket_connect(
            MEDIA_STREAM_PATH,
            headers={"X-Twilio-Signature": signature},
        ) as websocket:
            websocket.send_text(json.dumps(connected))
            websocket.send_text(json.dumps(start))
            close = websocket.receive()

    assert close == {"type": "websocket.close", "code": 1000, "reason": ""}
    assert repository.attempt_result_writes == []


@pytest.mark.asyncio
async def test_unknown_caller_gets_only_neutral_audio_and_hangup() -> None:
    repository = FakeRepository()
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 200
    assert "<Play>https://aurvek.example/webhooks/twilio/voice/private-audio/" in response.text
    assert "<Hangup" in response.text
    assert "<Connect" not in response.text


@pytest.mark.asyncio
async def test_disabled_inbound_route_gets_specific_cached_audio_and_hangup() -> None:
    class InboundUnavailableRepository(FakeRepository):
        async def create_inbound_call(self, **values):
            self.inbound.append(values)
            raise TelephonyInboundUnavailableError("incoming disabled")

    repository = InboundUnavailableRepository()
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))
        play_url = ElementTree.fromstring(response.text).findtext("Play")
        assert play_url is not None
        audio = await client.get(play_url)

    assert response.status_code == 200
    assert "/webhooks/twilio/voice/private-inbound-unavailable-audio/" in play_url
    assert "/private-audio/" not in play_url
    assert "<Hangup" in response.text
    assert "<Connect" not in response.text
    assert audio.status_code == 200
    assert audio.headers["cache-control"] == "private, no-store, max-age=0"
    assert audio.content.startswith(b"RIFF")


@pytest.mark.asyncio
async def test_unknown_caller_audio_is_private_tokenized_and_tamper_safe() -> None:
    repository = FakeRepository()
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))
        play_url = ElementTree.fromstring(response.text).findtext("Play")
        assert play_url is not None
        audio = await client.get(play_url)
        tampered = await client.get(play_url[:-1] + ("A" if play_url[-1] != "A" else "B"))

    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.headers["cache-control"] == "private, no-store, max-age=0"
    assert audio.content.startswith(b"RIFF")
    assert tampered.status_code == 404


def test_private_audio_token_is_bounded_and_expires() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    token = _build_private_audio_token(42, secret=AUTH_TOKEN, now=now)

    assert _parse_private_audio_token(token, secret=AUTH_TOKEN, now=now) == 42
    with pytest.raises(HTTPException) as expired:
        _parse_private_audio_token(
            token,
            secret=AUTH_TOKEN,
            now=now + timedelta(minutes=6),
        )
    assert expired.value.status_code == 404


def test_private_call_audio_token_is_call_cache_revision_scoped_and_expires() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    token = _build_private_call_audio_token(
        purpose="reconnect_failed",
        dispatch_token=TOKEN,
        call_id="call-1",
        cache_id=84,
        audio_revision=9,
        stream_attempt=2,
        secret=AUTH_TOKEN,
        now=now,
    )

    assert _parse_private_call_audio_token(
        token, secret=AUTH_TOKEN, now=now
    ) == {
        "purpose": "reconnect_failed",
        "dispatch_token": TOKEN,
        "call_id": "call-1",
        "cache_id": 84,
        "audio_revision": 9,
        "stream_attempt": 2,
    }
    with pytest.raises(HTTPException) as expired:
        _parse_private_call_audio_token(
            token,
            secret=AUTH_TOKEN,
            now=now + timedelta(minutes=6),
        )
    assert expired.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt", [0, 1])
async def test_connect_action_reconnects_only_first_two_stream_attempts(
    attempt,
) -> None:
    call = call_row()
    call["reconnect_count"] = attempt
    repository = FakeRepository(call)
    path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/{attempt}"
    form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 200
    assert response.text.count("<Connect") == 1
    assert f'value="{attempt + 1}"' in response.text
    assert "<Play>" not in response.text


@pytest.mark.asyncio
async def test_duplicate_attempt_zero_callback_never_hangs_up_active_attempt_one(
) -> None:
    call = call_row()
    repository = FakeRepository(call)
    path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/0"
    form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )
        duplicate = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert call["reconnect_count"] == 1
    for response in (first, duplicate):
        assert response.status_code == 200
        assert response.text.count("<Connect") == 1
        assert 'value="1"' in response.text
        assert "<Hangup" not in response.text


@pytest.mark.asyncio
async def test_concurrent_different_callbacks_replay_one_durable_continuation(
) -> None:
    call = call_row()
    repository = FakeRepository(call)
    gate = FakeConnectBillingGate()
    path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/0"
    first_form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    second_form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "4" * 32}
    transport = httpx.ASGITransport(
        app=app_for(repository, connect_billing_gate=gate)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first, second = await asyncio.gather(
            client.post(
                path,
                data=first_form,
                headers=signed_headers(path, first_form),
            ),
            client.post(
                path,
                data=second_form,
                headers=signed_headers(path, second_form),
            ),
        )

    assert first.status_code == second.status_code == 200
    assert first.text == second.text
    assert first.text.count("<Connect") == 1
    assert 'value="1"' in first.text
    assert "<Hangup" not in first.text
    assert call["reconnect_count"] == 1
    assert repository.reconnect_transitions == 1
    assert [item["stream_attempt"] for item in gate.calls] == [1, 1]
    assert all(item["include_pstn"] is False for item in gate.calls)


@pytest.mark.asyncio
async def test_stale_attempt_replays_its_original_continuation_without_mutation(
) -> None:
    call = call_row()
    call["reconnect_count"] = 2
    repository = FakeRepository(call)
    gate = FakeConnectBillingGate()
    path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/0"
    form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    transport = httpx.ASGITransport(
        app=app_for(repository, connect_billing_gate=gate)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 200
    assert response.text.count("<Connect") == 1
    assert 'value="1"' in response.text
    assert 'value="2"' not in response.text
    assert "<Hangup" not in response.text
    assert call["reconnect_count"] == 2
    assert repository.reconnect_transitions == 0
    assert gate.calls == [
        {
            "call_id": "call-1",
            "stream_attempt": 1,
            "call_elapsed_seconds": 0.0,
            "include_pstn": False,
            "include_stt": True,
            "stt_provider": "elevenlabs",
        }
    ]


@pytest.mark.asyncio
async def test_connect_action_attempt_is_covered_by_twilio_signature() -> None:
    repository = FakeRepository(call_row())
    signed_path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/0"
    tampered_path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/1"
    form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            tampered_path,
            data=form,
            headers=signed_headers(signed_path, form),
        )

    assert response.status_code == 403
    assert repository.call["reconnect_count"] == 0


@pytest.mark.asyncio
async def test_exhausted_disconnect_plays_exact_private_notice_then_hangs_up() -> None:
    call = call_row()
    call["reconnect_count"] = 2
    repository = FakeRepository(call)
    path = f"/webhooks/twilio/voice/connect-action/{TOKEN}/2"
    form = {"CallSid": CALL_SID, "StreamSid": "MZ" + "3" * 32}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )
        root = ElementTree.fromstring(response.text)
        play_url = root.findtext("Play")
        assert play_url is not None
        audio = await client.get(play_url)
        tampered = await client.get(
            play_url[:-1] + ("A" if play_url[-1] != "A" else "B")
        )
        wrong_purpose = _build_private_call_audio_token(
            purpose="balance_exhausted",
            dispatch_token=TOKEN,
            call_id="call-1",
            cache_id=84,
            audio_revision=9,
            stream_attempt=2,
            secret=AUTH_TOKEN,
        )
        crossed = await client.get(
            f"/webhooks/twilio/voice/call-audio/{wrong_purpose}"
        )

    assert response.status_code == 200
    assert root.find("Hangup") is not None
    assert root.find("Connect") is None
    assert audio.status_code == 200
    assert audio.content.startswith(b"RIFF")
    assert audio.headers["cache-control"] == "private, no-store, max-age=0"
    assert tampered.status_code == 404
    assert crossed.status_code == 404
    assert len(repository.hangup_requests) == 1
    assert repository.hangup_requests[0]["reason"] == "reconnect_failed"
    assert repository.hangup_requests[0]["target_status"] is PhoneCallStatus.FAILED


@pytest.mark.asyncio
async def test_private_call_audio_rejects_expired_other_call_and_other_cache() -> None:
    call = call_row()
    call["reconnect_count"] = 2
    repository = FakeRepository(call)
    expired = _build_private_call_audio_token(
        purpose="reconnect_failed",
        dispatch_token=TOKEN,
        call_id="call-1",
        cache_id=84,
        audio_revision=9,
        stream_attempt=2,
        secret=AUTH_TOKEN,
        now=datetime.now(UTC) - timedelta(minutes=6),
    )
    other_call = _build_private_call_audio_token(
        purpose="reconnect_failed",
        dispatch_token=TOKEN,
        call_id="call-other",
        cache_id=84,
        audio_revision=9,
        stream_attempt=2,
        secret=AUTH_TOKEN,
    )
    other_cache = _build_private_call_audio_token(
        purpose="reconnect_failed",
        dispatch_token=TOKEN,
        call_id="call-1",
        cache_id=999,
        audio_revision=9,
        stream_attempt=2,
        secret=AUTH_TOKEN,
    )
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        responses = [
            await client.get(f"/webhooks/twilio/voice/call-audio/{token}")
            for token in (expired, other_call, other_cache)
        ]

    assert [response.status_code for response in responses] == [404, 404, 404]


@pytest.mark.asyncio
async def test_provider_webhooks_stay_unavailable_when_cache_backend_is_unwired() -> None:
    repository = FakeRepository(call_row())
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(
        app=app_for(repository, notice_loader=None)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 503
    assert repository.inbound == []


@pytest.mark.asyncio
async def test_standard_inbound_call_fails_its_route_without_scribe_provider() -> None:
    repository = FakeRepository(call_row())
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(
        app=app_for(repository, elevenlabs_api_key_provider=None)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 503
    assert len(repository.inbound) == 1


@pytest.mark.asyncio
async def test_signed_late_status_reconciles_even_when_runtime_is_unwired() -> None:
    repository = FakeRepository(call_row())
    path = f"/webhooks/twilio/voice/status/{TOKEN}"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "SequenceNumber": "9",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, notice_loader=None)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 204
    assert repository.statuses[0][0].status.value == "completed"


@pytest.mark.asyncio
async def test_known_inbound_returns_only_bidirectional_media_stream_twiml() -> None:
    repository = FakeRepository(call_row())
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 200
    assert response.text.count("<Connect") == 1
    assert "wss://aurvek.example/ws/twilio/media-stream" in response.text
    assert f'value="{TOKEN}"' in response.text
    assert "<Gather" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "path", "expected_include_pstn"),
    (
        ("inbound", "/webhooks/twilio/voice/inbound", True),
        ("outbound", f"/webhooks/twilio/voice/twiml/{TOKEN}", False),
    ),
)
async def test_initial_connect_gate_scopes_global_pstn_by_direction(
    direction, path, expected_include_pstn
) -> None:
    call = call_row()
    call["direction"] = direction
    repository = FakeRepository(call)
    gate = FakeConnectBillingGate()
    form = {"CallSid": CALL_SID}
    if direction == "inbound":
        form.update({"From": "+13055550100", "To": "+13055550999"})
    transport = httpx.ASGITransport(
        app=app_for(repository, connect_billing_gate=gate)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 200
    assert response.text.count("<Connect") == 1
    assert gate.calls == [
        {
            "call_id": "call-1",
            "stream_attempt": 0,
            "call_elapsed_seconds": 0.0,
            "include_pstn": expected_include_pstn,
            "include_stt": True,
            "stt_provider": "elevenlabs",
        }
    ]


@pytest.mark.asyncio
async def test_realtime_connect_uses_openai_readiness_and_omits_scribe_gate(
) -> None:
    call = call_row()
    snapshot = json.loads(call["config_snapshot_json"])
    snapshot.update(
        runtime_kind="openai_realtime",
        runtime_model="gpt-realtime-2.1-mini",
        phone_realtime_voice="marin",
    )
    call["config_snapshot_json"] = json.dumps(snapshot)
    repository = FakeRepository(call)
    gate = FakeConnectBillingGate()
    path = f"/webhooks/twilio/voice/twiml/{TOKEN}"
    form = {"CallSid": CALL_SID}
    transport = httpx.ASGITransport(
        app=app_for(
            repository,
            elevenlabs_api_key_provider=None,
            openai_api_key_provider=lambda: "fake-openai",
            connect_billing_gate=gate,
        )
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 200
    assert response.text.count("<Connect") == 1
    assert gate.calls == [
        {
            "call_id": "call-1",
            "stream_attempt": 0,
            "call_elapsed_seconds": 0.0,
            "include_pstn": False,
            "include_stt": True,
            "stt_provider": "openai",
        }
    ]


@pytest.mark.asyncio
async def test_connect_billing_failure_never_emits_connect() -> None:
    repository = FakeRepository(call_row())
    gate = FakeConnectBillingGate(PhoneBillingError("temporary billing failure"))
    path = "/webhooks/twilio/voice/inbound"
    form = {"CallSid": CALL_SID, "From": "+13055550100", "To": "+13055550999"}
    transport = httpx.ASGITransport(
        app=app_for(repository, connect_billing_gate=gate)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 503
    assert "<Connect" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "path"),
    (
        ("inbound", "/webhooks/twilio/voice/inbound"),
        ("outbound", f"/webhooks/twilio/voice/twiml/{TOKEN}"),
    ),
)
async def test_connect_balance_exhaustion_audio_is_fetchable_and_purpose_scoped(
    direction, path
) -> None:
    call = call_row()
    call["direction"] = direction
    repository = FakeRepository(call)
    gate = FakeConnectBillingGate(PhoneBillingExhausted("no balance"))
    form = {"CallSid": CALL_SID}
    if direction == "inbound":
        form.update({"From": "+13055550100", "To": "+13055550999"})
    transport = httpx.ASGITransport(
        app=app_for(repository, connect_billing_gate=gate)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )
        play_url = ElementTree.fromstring(response.text).findtext("Play")
        assert play_url is not None
        audio = await client.get(play_url)
        wrong_purpose = _build_private_call_audio_token(
            purpose="reconnect_failed",
            dispatch_token=TOKEN,
            call_id="call-1",
            cache_id=84,
            audio_revision=9,
            stream_attempt=0,
            secret=AUTH_TOKEN,
        )
        crossed = await client.get(
            f"/webhooks/twilio/voice/call-audio/{wrong_purpose}"
        )
        forged = await client.get(
            play_url[:-1] + ("A" if play_url[-1] != "A" else "B")
        )

    assert response.status_code == 200
    assert "<Connect" not in response.text
    assert "<Play>" in response.text
    assert "<Hangup" in response.text
    assert repository.hangup_requests[0]["reason"] == "balance_exhausted"
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/wav")
    assert audio.content.startswith(b"RIFF")
    assert crossed.status_code == 404
    assert forged.status_code == 404


@pytest.mark.asyncio
async def test_static_inbound_status_correlates_by_sid_without_disclosing_presence() -> None:
    call = call_row()
    repository = FakeRepository(call)
    billing = FakeBillingService()
    path = "/webhooks/twilio/voice/inbound-status"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "CallDuration": "73",
        "SequenceNumber": "9",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing, notice_loader=None)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        found = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )
        repository.call = None
        unknown_form = {**form, "CallSid": "CA" + "9" * 32}
        missing = await client.post(
            path,
            data=unknown_form,
            headers=signed_headers(path, unknown_form),
        )

    assert found.status_code == missing.status_code == 204
    assert repository.statuses[0][1] is None
    assert billing.duration_calls == [
        {
            "call_id": "call-1",
            "component_type": "pstn",
            "duration_seconds": 73,
            "external_usage_id": f"twilio:call-duration:{CALL_SID}",
        }
    ]


@pytest.mark.asyncio
async def test_static_inbound_status_signature_covers_exact_callback_url() -> None:
    repository = FakeRepository(call_row())
    path = "/webhooks/twilio/voice/inbound-status"
    different_path = "/webhooks/twilio/voice/inbound"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "SequenceNumber": "9",
    }
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path,
            data=form,
            headers=signed_headers(different_path, form),
        )

    assert response.status_code == 403
    assert repository.statuses == []


@pytest.mark.asyncio
async def test_static_inbound_status_does_not_cross_apply_to_outbound_call() -> None:
    call = call_row()
    call["direction"] = "outbound"
    repository = FakeRepository(call)
    billing = FakeBillingService()
    path = "/webhooks/twilio/voice/inbound-status"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "CallDuration": "73",
        "SequenceNumber": "9",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 204
    assert billing.duration_calls == []


@pytest.mark.asyncio
async def test_signed_status_uses_dispatch_token_for_callback_before_rest() -> None:
    repository = FakeRepository(call_row())
    path = f"/webhooks/twilio/voice/status/{TOKEN}"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "initiated",
        "SequenceNumber": "0",
    }
    transport = httpx.ASGITransport(app=app_for(repository))
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(path, data=form, headers=signed_headers(path, form))

    assert response.status_code == 204
    assert len(repository.statuses) == 1
    assert repository.statuses[0][0].call_sid == CALL_SID
    assert repository.statuses[0][1] == TOKEN


@pytest.mark.asyncio
async def test_terminal_status_retries_billing_after_event_dedupe() -> None:
    class Repository(FakeRepository):
        async def record_call_status(
            self,
            event,
            *,
            dispatch_token=None,
            expected_direction=None,
        ):
            self.statuses.append((event, dispatch_token))
            return self.call, len(self.statuses) == 1

    repository = Repository(call_row())
    billing = FakeBillingService()
    billing.duration_errors.append(PhoneBillingError("temporary reconciliation"))
    path = f"/webhooks/twilio/voice/status/{TOKEN}"
    form = {
        "CallSid": CALL_SID,
        "CallStatus": "completed",
        "CallDuration": "73",
        "SequenceNumber": "9",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first = await client.post(path, data=form, headers=signed_headers(path, form))
        second = await client.post(path, data=form, headers=signed_headers(path, form))

    assert [first.status_code, second.status_code] == [503, 204]
    assert len(repository.statuses) == 2
    assert billing.duration_calls == [
        {
            "call_id": "call-1",
            "component_type": "pstn",
            "duration_seconds": 73,
            "external_usage_id": f"twilio:call-duration:{CALL_SID}",
        },
        {
            "call_id": "call-1",
            "component_type": "pstn",
            "duration_seconds": 73,
            "external_usage_id": f"twilio:call-duration:{CALL_SID}",
        },
    ]


@pytest.mark.asyncio
async def test_machine_callback_retries_ambiguous_hangup_until_accepted() -> None:
    call = call_row()
    call["status"] = "in_progress"
    repository = DurableHangupRepository(call)
    voice = SequencedVoiceClient([RuntimeError("timeout"), True])
    path = f"/webhooks/twilio/voice/amd/{TOKEN}"
    form = {"CallSid": CALL_SID, "AnsweredBy": "machine_start"}
    transport = httpx.ASGITransport(
        app=app_for(repository, voice_client=voice)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first = await client.post(path, data=form, headers=signed_headers(path, form))
        second = await client.post(path, data=form, headers=signed_headers(path, form))

    assert first.status_code == 503
    assert second.status_code == 204
    assert voice.calls == [CALL_SID, CALL_SID]
    assert repository.actions == [
        ("event", "amd"),
        ("requested", "machine"),
        ("unresolved", "machine"),
        ("event", "amd"),
        ("requested", "machine"),
        ("accepted", "machine"),
    ]


@pytest.mark.asyncio
async def test_machine_callback_hangs_up_even_when_billing_needs_retry() -> None:
    call = call_row()
    repository = DurableHangupRepository(call)
    voice = SequencedVoiceClient([True, True])
    billing = FakeBillingService()
    billing.amd_errors.append(RuntimeError("temporary AMD database failure"))
    path = f"/webhooks/twilio/voice/amd/{TOKEN}"
    first_form = {"CallSid": CALL_SID, "AnsweredBy": "machine_start"}
    second_form = {
        "CallSid": CALL_SID,
        "AnsweredBy": "machine_end_beep",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, voice_client=voice, billing_service=billing)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first = await client.post(
            path, data=first_form, headers=signed_headers(path, first_form)
        )
        second = await client.post(
            path, data=second_form, headers=signed_headers(path, second_form)
        )

    assert [first.status_code, second.status_code] == [503, 204]
    assert voice.calls == [CALL_SID, CALL_SID]
    assert len(billing.amd_calls) == 2
    assert billing.amd_calls[0]["external_usage_id"] != billing.amd_calls[1][
        "external_usage_id"
    ]


@pytest.mark.asyncio
async def test_recording_duration_retries_with_stable_recording_identity() -> None:
    class Repository(FakeRepository):
        def __init__(self, call):
            super().__init__(call)
            self.recordings = []

        async def record_recording_status(self, event, *, dispatch_token=None):
            assert dispatch_token == TOKEN
            self.recordings.append(event)
            return self.call, len(self.recordings) == 1

    repository = Repository(call_row())
    billing = FakeBillingService()
    billing.duration_errors.append(PhoneBillingError("temporary recording billing"))
    path = f"/webhooks/twilio/voice/recording/{TOKEN}"
    recording_sid = "RE" + "4" * 32
    form = {
        "CallSid": CALL_SID,
        "RecordingSid": recording_sid,
        "RecordingStatus": "completed",
        "RecordingDuration": "31",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        first = await client.post(path, data=form, headers=signed_headers(path, form))
        second = await client.post(path, data=form, headers=signed_headers(path, form))

    assert [first.status_code, second.status_code] == [503, 204]
    assert len(repository.recordings) == 2
    assert [item["external_usage_id"] for item in billing.duration_calls] == [
        f"twilio:recording-duration:{recording_sid}",
        f"twilio:recording-duration:{recording_sid}",
    ]


@pytest.mark.asyncio
async def test_deleted_callbacks_never_recreate_billing() -> None:
    class DeletedCallbacks:
        async def is_deleted_provider_call(self, _call_sid):
            return False

        async def is_deleted_callback(self, _token, _call_sid):
            return True

        async def capture_late_recording(self, **_values):
            return True

    class Repository(FakeRepository):
        async def record_recording_status(self, event, *, dispatch_token=None):
            return None, False

    repository = Repository(call_row())
    billing = FakeBillingService()
    purge = DeletedCallbacks()
    requests = (
        (
            f"/webhooks/twilio/voice/status/{TOKEN}",
            {"CallSid": CALL_SID, "CallStatus": "completed", "CallDuration": "4"},
        ),
        (
            f"/webhooks/twilio/voice/amd/{TOKEN}",
            {"CallSid": CALL_SID, "AnsweredBy": "human"},
        ),
        (
            f"/webhooks/twilio/voice/recording/{TOKEN}",
            {
                "CallSid": CALL_SID,
                "RecordingSid": "RE" + "5" * 32,
                "RecordingStatus": "completed",
                "RecordingDuration": "4",
            },
        ),
    )
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing, purge_repository=purge)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        responses = [
            await client.post(path, data=form, headers=signed_headers(path, form))
            for path, form in requests
        ]

    assert [response.status_code for response in responses] == [204, 204, 204]
    assert repository.statuses == []
    assert billing.duration_calls == []
    assert billing.amd_calls == []


@pytest.mark.asyncio
async def test_amd_tombstone_race_is_rechecked_after_call_disappears() -> None:
    class PurgeRace:
        def __init__(self):
            self.checks = 0

        async def is_deleted_provider_call(self, _call_sid):
            return False

        async def is_deleted_callback(self, _token, _call_sid):
            self.checks += 1
            return self.checks == 2

        async def capture_late_recording(self, **_values):
            return False

    purge = PurgeRace()
    billing = FakeBillingService()
    repository = FakeRepository(None)
    path = f"/webhooks/twilio/voice/amd/{TOKEN}"
    form = {"CallSid": CALL_SID, "AnsweredBy": "human"}
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing, purge_repository=purge)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 204
    assert purge.checks == 2
    assert billing.amd_calls == []


@pytest.mark.asyncio
async def test_recording_tombstone_race_uses_atomic_repository_result() -> None:
    class PurgeRace:
        def __init__(self):
            self.captures = 0

        async def is_deleted_provider_call(self, _call_sid):
            return False

        async def is_deleted_callback(self, _token, _call_sid):
            return False

        async def capture_late_recording(self, **_values):
            self.captures += 1
            return self.captures == 2

    purge = PurgeRace()
    billing = FakeBillingService()

    class Repository(FakeRepository):
        def __init__(self):
            super().__init__(None)
            self.recordings = []

        async def record_recording_status(self, event, *, dispatch_token=None):
            self.recordings.append((event, dispatch_token))
            return None, False

    repository = Repository()
    path = f"/webhooks/twilio/voice/recording/{TOKEN}"
    form = {
        "CallSid": CALL_SID,
        "RecordingSid": "RE" + "6" * 32,
        "RecordingStatus": "completed",
        "RecordingDuration": "9",
    }
    transport = httpx.ASGITransport(
        app=app_for(repository, billing_service=billing, purge_repository=purge)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="https://aurvek.example"
    ) as client:
        response = await client.post(
            path, data=form, headers=signed_headers(path, form)
        )

    assert response.status_code == 204
    assert purge.captures == 0
    assert len(repository.recordings) == 1
    assert repository.recordings[0][1] == TOKEN
    assert billing.duration_calls == []


@pytest.mark.asyncio
async def test_stale_hangup_acceptance_is_not_inferred_as_callback_confirmation() -> None:
    call = call_row()
    call["status"] = "in_progress"

    class Repository(DurableHangupRepository):
        async def mark_hangup_accepted(self, **_values):
            self.actions.append(("accepted-stale", "machine"))
            return False

        async def get_hangup_attempt_state(self, **_values):
            return "in_flight"

        async def mark_hangup_unresolved(self, **values):
            self.actions.append(("unresolved-stale", values["reason"]))
            return False

    repository = Repository(call)
    voice = SequencedVoiceClient([True])
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        voice_client=voice,
    )

    with pytest.raises(TelephonyStateError, match="lost its durable fence"):
        await runtime.hangup_durable(
            call,
            reason="machine",
            target_status=PhoneCallStatus.MACHINE,
            origin="amd",
        )

    assert voice.calls == [CALL_SID]
    assert repository.actions == [
        ("requested", "machine"),
        ("accepted-stale", "machine"),
        ("unresolved-stale", "machine"),
    ]


@pytest.mark.asyncio
async def test_stale_unresolved_token_does_not_hide_provider_ambiguity() -> None:
    call = call_row()
    call["status"] = "in_progress"

    class Repository(DurableHangupRepository):
        async def mark_hangup_unresolved(self, **values):
            self.actions.append(("unresolved-stale", values["reason"]))
            return False

        async def get_hangup_attempt_state(self, **_values):
            return "in_flight"

    repository = Repository(call)
    voice = SequencedVoiceClient([RuntimeError("timeout")])
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        voice_client=voice,
    )

    with pytest.raises(RuntimeError, match="timeout"):
        await runtime.hangup_durable(
            call,
            reason="machine",
            target_status=PhoneCallStatus.MACHINE,
            origin="amd",
        )

    assert voice.calls == [CALL_SID]
    assert repository.actions == [
        ("requested", "machine"),
        ("unresolved-stale", "machine"),
    ]


@pytest.mark.asyncio
async def test_provider_absent_hangup_uses_definitive_fenced_reconciliation() -> None:
    call = call_row()
    call["status"] = "in_progress"

    class Repository(DurableHangupRepository):
        async def reconcile_hangup_provider_absent(self, **_values):
            self.actions.append(("provider-absent", "machine"))
            return True

    repository = Repository(call)
    voice = SequencedVoiceClient([False])
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        voice_client=voice,
    )

    await runtime.hangup_durable(
        call,
        reason="machine",
        target_status=PhoneCallStatus.MACHINE,
        origin="amd",
    )

    assert voice.calls == [CALL_SID]
    assert repository.actions == [
        ("requested", "machine"),
        ("provider-absent", "machine"),
    ]


@pytest.mark.asyncio
async def test_invalid_hangup_adapter_result_is_unresolved_not_accepted() -> None:
    call = call_row()
    call["status"] = "in_progress"
    repository = DurableHangupRepository(call)
    voice = SequencedVoiceClient([None])
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        voice_client=voice,
    )

    with pytest.raises(RuntimeError, match="invalid result"):
        await runtime.hangup_durable(
            call,
            reason="machine",
            target_status=PhoneCallStatus.MACHINE,
            origin="amd",
        )

    assert voice.calls == [CALL_SID]
    assert repository.actions == [
        ("requested", "machine"),
        ("unresolved", "machine"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_result", [True, False])
async def test_post_rest_persistence_failure_is_fenced_unresolved(
    provider_result,
) -> None:
    call = call_row()
    call["status"] = "in_progress"

    class Repository(DurableHangupRepository):
        async def mark_hangup_accepted(self, **_values):
            self.actions.append(("accepted-write-failed", "machine"))
            raise RuntimeError("accepted write failed")

        async def reconcile_hangup_provider_absent(self, **_values):
            self.actions.append(("absent-write-failed", "machine"))
            raise RuntimeError("absent write failed")

    repository = Repository(call)
    voice = SequencedVoiceClient([provider_result])
    runtime = TelephonyProviderRuntime(
        account_sid=ACCOUNT_SID,
        auth_token=AUTH_TOKEN,
        elevenlabs_api_key_provider=lambda: "fake-elevenlabs",
        repository=repository,
        readiness_check=ready,
        voice_client=voice,
    )

    with pytest.raises(RuntimeError, match="write failed"):
        await runtime.hangup_durable(
            call,
            reason="machine",
            target_status=PhoneCallStatus.MACHINE,
            origin="amd",
        )

    expected_write = (
        "accepted-write-failed" if provider_result else "absent-write-failed"
    )
    assert voice.calls == [CALL_SID]
    assert repository.actions == [
        ("requested", "machine"),
        (expected_write, "machine"),
        ("unresolved", "machine"),
    ]


def test_start_peek_is_bounded_and_only_extracts_lookup_fields() -> None:
    raw = (
        '{"event":"start","start":{"callSid":"%s",'
        '"customParameters":{"correlation_token":"%s","stream_attempt":"2"}}}'
        % (CALL_SID, TOKEN)
    )
    assert _peek_start(raw) == {
        "call_sid": CALL_SID,
        "correlation_token": TOKEN,
        "stream_attempt": 2,
    }
    with pytest.raises(ValueError):
        _peek_start("{}")
    with pytest.raises(ValueError):
        _peek_start("x" * 262_145)
