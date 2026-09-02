from __future__ import annotations

import asyncio
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from ai_runtime.voice_resolution import CanonicalVoice
from integrations.telephony.audio import PcmuCacheAsset
from integrations.telephony.snapshot import (
    ConversationPhoneSnapshot,
    ELEVENLABS_PHONE_TTS_MODEL_ID,
    ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
    PhoneSnapshotError,
    canonical_voice_from_snapshot,
    live_tts_profile_from_snapshot,
    tts_profile_from_snapshot,
)
from integrations.telephony import speech
from integrations.telephony.billing import PhoneBillingExhausted
from integrations.telephony.speech import (
    PhoneSpeechBillingExhausted,
    PhoneSpeechError,
    PhoneTextFragmenter,
)
from tools.tts_config import TTSProfile


def _install_fake_elevenlabs_http(
    monkeypatch,
    *,
    status=200,
    headers=None,
    chunks=(),
    enter_error=None,
):
    captured = {}

    class FakeContent:
        async def iter_chunked(self, size):
            captured["read_size"] = size
            for chunk in chunks:
                yield chunk

    class FakeResponse:
        content = FakeContent()

        def __init__(self):
            self.status = status
            self.headers = dict(headers or {})

        async def __aenter__(self):
            if enter_error is not None:
                raise enter_error
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeSession:
        def __init__(self, *, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return FakeResponse()

    monkeypatch.setattr(speech.aiohttp, "ClientSession", FakeSession)
    return captured


def _snapshot(*, provider: str = "elevenlabs") -> dict:
    if provider == "elevenlabs":
        voice_code = "canonical-voice"
        service_name = "TTS-ElevenLabs"
    else:
        voice_code = "alloy"
        service_name = "TTS-OpenAI"
    snapshot = ConversationPhoneSnapshot(
        conversation_id=7,
        owner_user_id=3,
        prompt_id=11,
        llm_id=13,
        runtime_llm_id=13,
        runtime_kind="standard",
        runtime_model="gpt-4.1-mini",
        reasoning_selection={"mode": "default"},
        phone_realtime_voice=None,
        canonical_voice=CanonicalVoice(
            id=17,
            voice_code=voice_code,
            name="Canonical",
            tts_service=19,
            service_name=service_name,
            provider=provider,
            inherited_default=False,
        ),
        tts_profile=TTSProfile(
            "snapshot-model",
            "mp3_44100_128",
            0.4,
            0.8,
            False,
            [120, 160],
        ),
        phone_settings={
            "recording_default": False,
            "amd_default": False,
            "stt_locale": "multi",
        },
        audio_revision=23,
        captured_at="2026-08-31T03:04:05Z",
    )
    return snapshot.as_dict()


def test_snapshot_round_trips_exact_provider_voice_and_profile():
    values = _snapshot()

    voice = canonical_voice_from_snapshot(values)
    profile = tts_profile_from_snapshot(values)
    live_profile = live_tts_profile_from_snapshot(values)

    assert (voice.provider, voice.voice_code, voice.id) == (
        "elevenlabs",
        "canonical-voice",
        17,
    )
    assert profile.model_id == "snapshot-model"
    assert profile.output_format == "mp3_44100_128"
    assert profile.chunk_schedule == [120, 160]
    assert live_profile.model_id == ELEVENLABS_PHONE_TTS_MODEL_ID
    assert live_profile.output_format == ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT
    assert live_profile.stability == profile.stability
    assert live_profile.similarity_boost == profile.similarity_boost


def test_openai_snapshot_keeps_existing_live_tts_profile():
    values = _snapshot(provider="openai")

    cache_profile = tts_profile_from_snapshot(values)
    live_profile = live_tts_profile_from_snapshot(values)

    assert live_profile.model_id == cache_profile.model_id == "snapshot-model"
    assert (
        live_profile.output_format
        == cache_profile.output_format
        == "mp3_44100_128"
    )


def test_legacy_snapshot_without_live_profile_keeps_captured_provider_path():
    values = _snapshot()
    values.pop("live_tts_profile")

    legacy_profile = live_tts_profile_from_snapshot(values)

    assert legacy_profile.model_id == "snapshot-model"
    assert legacy_profile.output_format == "mp3_44100_128"


def test_snapshot_rejects_tampered_elevenlabs_live_profile():
    values = _snapshot()
    values["live_tts_profile"]["output_format"] = "mp3_44100_128"

    with pytest.raises(PhoneSnapshotError, match="live TTS profile"):
        live_tts_profile_from_snapshot(values)


def test_snapshot_rejects_provider_or_voice_identity_drift():
    values = _snapshot()
    values["provider_voice_id"] = "other-voice"

    with pytest.raises(PhoneSnapshotError, match="voice identity"):
        canonical_voice_from_snapshot(values)


def test_text_fragmenter_preserves_exact_prefixes_and_word_boundaries():
    fragmenter = PhoneTextFragmenter(min_chars=8, max_chars=24)

    first = fragmenter.feed("Hello there. This is a ")
    second = fragmenter.feed("longer sentence without a stop yet")
    final = fragmenter.finish()
    fragments = first + second + final

    assert "".join(fragments) == (
        "Hello there. This is a longer sentence without a stop yet"
    )
    assert fragments[0] == "Hello there. "
    assert all(not part.startswith(" ") for part in fragments[1:])


@pytest.mark.asyncio
async def test_elevenlabs_request_uses_flash_raw_ulaw_and_returns_exact_bytes(
    monkeypatch,
):
    captured = _install_fake_elevenlabs_http(
        monkeypatch,
        headers={"Content-Length": "4"},
        chunks=(b"\x01\x02", b"\xff\x00"),
    )

    pcmu = await speech._request_elevenlabs_phone_pcmu(
        voice_id="voice/with space",
        text="Hola.",
        api_key="not-logged-test-key",
        model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
        output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
        stability=0.4,
        similarity_boost=0.8,
    )

    assert pcmu == b"\x01\x02\xff\x00"
    assert captured["url"].endswith("/voice%2Fwith%20space/stream")
    assert captured["params"] == {"output_format": "ulaw_8000"}
    assert captured["json"] == {
        "text": "Hola.",
        "model_id": "eleven_flash_v2_5",
        "voice_settings": {"stability": 0.4, "similarity_boost": 0.8},
    }
    assert captured["allow_redirects"] is False


@pytest.mark.asyncio
async def test_elevenlabs_request_forwards_each_validated_pcmu_chunk(monkeypatch):
    _install_fake_elevenlabs_http(
        monkeypatch,
        headers={"Content-Length": "4"},
        chunks=(b"\x01\x02", b"\xff\x00"),
    )
    forwarded = []

    async def consume(chunk):
        forwarded.append(chunk)

    pcmu = await speech._request_elevenlabs_phone_pcmu(
        voice_id="voice-id",
        text="Hola.",
        api_key="test-key",
        model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
        output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
        stability=0.4,
        similarity_boost=0.8,
        on_pcmu_chunk=consume,
    )

    assert forwarded == [b"\x01\x02", b"\xff\x00"]
    assert b"".join(forwarded) == pcmu


@pytest.mark.asyncio
async def test_elevenlabs_key_selection_uses_exact_voice_off_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    captured = {}

    def exact_key(*, voice_id):
        captured["voice_id"] = voice_id
        captured["thread"] = threading.get_ident()
        return "voice-specific-key"

    monkeypatch.setattr(speech, "get_elevenlabs_key", exact_key)

    selected = await speech._select_elevenlabs_phone_key("exact-canonical-voice")

    assert selected == "voice-specific-key"
    assert captured["voice_id"] == "exact-canonical-voice"
    assert captured["thread"] != main_thread


@pytest.mark.asyncio
async def test_elevenlabs_key_selection_has_deterministic_outer_deadline(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_key(*, voice_id):
        nonlocal calls
        calls += 1
        assert voice_id == "slow-voice"
        entered.set()
        release.wait()
        return "eventual-key"

    monkeypatch.setattr(speech, "get_elevenlabs_key", blocked_key)
    monkeypatch.setattr(
        speech,
        "ELEVENLABS_PHONE_KEY_SELECTION_TIMEOUT_SECONDS",
        0.01,
    )

    try:
        with pytest.raises(PhoneSpeechError, match="selection timed out"):
            await asyncio.wait_for(
                speech._select_elevenlabs_phone_key("slow-voice"),
                timeout=0.2,
            )
        for _ in range(1_000):
            if entered.is_set():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("timed-out sync key probe never started")
        assert entered.is_set()
        assert calls == 1
        with speech._ELEVENLABS_KEY_PROBE_GUARD:
            assert set(speech._ELEVENLABS_KEY_PROBE_FLIGHTS) == {"slow-voice"}

        # The timed-out waiter reuses the still-running probe instead of
        # submitting a replacement thread that cancellation cannot stop.
        with pytest.raises(PhoneSpeechError, match="selection timed out"):
            await speech._select_elevenlabs_phone_key("slow-voice")
        assert calls == 1
    finally:
        release.set()

    for _ in range(1_000):
        with speech._ELEVENLABS_KEY_PROBE_GUARD:
            if not speech._ELEVENLABS_KEY_PROBE_FLIGHTS:
                break
        await asyncio.sleep(0.001)
    else:
        raise AssertionError("completed key probe did not release shared capacity")


@pytest.mark.asyncio
async def test_cancelled_sync_key_probe_keeps_bounded_capacity_until_thread_exits(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocked_key(*, voice_id):
        nonlocal calls
        calls += 1
        entered.set()
        release.wait()
        return f"key-for-{voice_id}"

    monkeypatch.setattr(speech, "get_elevenlabs_key", blocked_key)
    monkeypatch.setattr(speech, "_ELEVENLABS_KEY_PROBE_CAPACITY", 1)
    monkeypatch.setattr(
        speech,
        "ELEVENLABS_PHONE_KEY_SELECTION_TIMEOUT_SECONDS",
        1.0,
    )

    first = asyncio.create_task(
        speech._select_elevenlabs_phone_key("occupied-voice")
    )
    for _ in range(1_000):
        if entered.is_set():
            break
        await asyncio.sleep(0.001)
    else:
        release.set()
        raise AssertionError("sync key probe did not start")

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    try:
        for index in range(10):
            with pytest.raises(PhoneSpeechError, match="capacity"):
                await speech._select_elevenlabs_phone_key(f"queued-{index}")
        assert calls == 1
        with speech._ELEVENLABS_KEY_PROBE_GUARD:
            assert set(speech._ELEVENLABS_KEY_PROBE_FLIGHTS) == {
                "occupied-voice"
            }
    finally:
        release.set()

    for _ in range(1_000):
        with speech._ELEVENLABS_KEY_PROBE_GUARD:
            if not speech._ELEVENLABS_KEY_PROBE_FLIGHTS:
                break
        await asyncio.sleep(0.001)
    else:
        raise AssertionError("cancelled key probe did not release after thread exit")


@pytest.mark.asyncio
async def test_elevenlabs_http_status_is_bounded_and_does_not_expose_key(monkeypatch):
    _install_fake_elevenlabs_http(
        monkeypatch,
        status=503,
        chunks=(b"provider diagnostic that must not escape",),
    )

    with pytest.raises(PhoneSpeechError, match="HTTP 503") as caught:
        await speech._request_elevenlabs_phone_pcmu(
            voice_id="canonical-voice",
            text="Hola.",
            api_key="super-secret-test-key",
            model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
            output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
            stability=0.4,
            similarity_boost=0.8,
        )

    assert "super-secret-test-key" not in str(caught.value)
    assert "provider diagnostic" not in str(caught.value)


@pytest.mark.asyncio
async def test_elevenlabs_http_timeout_is_generic(monkeypatch):
    _install_fake_elevenlabs_http(
        monkeypatch,
        enter_error=asyncio.TimeoutError(),
    )

    with pytest.raises(PhoneSpeechError, match="request failed"):
        await speech._request_elevenlabs_phone_pcmu(
            voice_id="canonical-voice",
            text="Hola.",
            api_key="test-key",
            model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
            output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
            stability=0.4,
            similarity_boost=0.8,
        )


@pytest.mark.asyncio
async def test_elevenlabs_http_rejects_oversized_stream(monkeypatch):
    monkeypatch.setattr(speech, "MAX_ELEVENLABS_PHONE_AUDIO_BYTES", 3)
    _install_fake_elevenlabs_http(
        monkeypatch,
        chunks=(b"1234",),
    )

    with pytest.raises(PhoneSpeechError, match="size limit"):
        await speech._request_elevenlabs_phone_pcmu(
            voice_id="canonical-voice",
            text="Hola.",
            api_key="test-key",
            model_id=ELEVENLABS_PHONE_TTS_MODEL_ID,
            output_format=ELEVENLABS_PHONE_TTS_OUTPUT_FORMAT,
            stability=0.4,
            similarity_boost=0.8,
        )


class _BillingService:
    def __init__(self, *, exhausted: bool = False):
        self.events = []
        self.exhausted = exhausted
        self.component = SimpleNamespace(id=41, state="reserved")

    async def record_cache_hit(self, **values):
        self.events.append(("cache_hit", values))

    async def reserve_component(self, **values):
        self.events.append(("reserve", values))
        if self.exhausted:
            raise PhoneBillingExhausted("test balance")
        return self.component

    async def claim_provider_start(self, component_id):
        self.events.append(("provider_started", component_id))
        return self.component

    async def settle_component(self, component_id):
        self.events.append(("settle", component_id))

    async def mark_ambiguous(self, component_id, *, reason):
        self.events.append(("ambiguous", component_id, reason))

    async def refund_component(self, component_id, *, reason):
        self.events.append(("refund", component_id, reason))


@pytest.mark.asyncio
async def test_elevenlabs_renderer_preserves_raw_pcmu_and_reuses_private_cache(
    monkeypatch,
    tmp_path,
):
    captured = {}
    provider_calls = 0
    raw_pcmu = b"\xff\x7f\x00\x81" * 40
    billing = _BillingService()
    stream_events = []

    async def fake_key(voice_id):
        captured["voice_id"] = voice_id
        return "test-key"

    async def fake_request(**kwargs):
        nonlocal provider_calls
        provider_calls += 1
        captured.update(kwargs)
        callback = kwargs.get("on_pcmu_chunk")
        if callback is not None:
            await callback(raw_pcmu[:80])
            await callback(raw_pcmu[80:])
        return raw_pcmu

    async def on_chunk(chunk):
        stream_events.append(("chunk", chunk))

    async def on_complete(pcmu):
        stream_events.append(("complete", pcmu))

    async def cache_hit_must_not_stream(_audio):
        raise AssertionError("cache hit must not invoke streaming callbacks")

    async def legacy_must_not_run(*_args, **_kwargs):
        raise AssertionError("native ElevenLabs phone TTS must bypass legacy TTS")

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(speech, "_request_elevenlabs_phone_pcmu", fake_request)
    monkeypatch.setattr(speech, "handle_tts_request", legacy_must_not_run)

    first = await speech.render_phone_speech(
        text="Exact audible text.",
        conversation_id=7,
        current_user=SimpleNamespace(id=3),
        call_snapshot=_snapshot(),
        cache_root=tmp_path / "private-cache",
        call_id="call-1",
        billing_dedupe_key="tts:1",
        billing_service=billing,
        on_pcmu_chunk=on_chunk,
        on_pcmu_complete=on_complete,
    )
    second = await speech.render_phone_speech(
        text="Exact audible text.",
        conversation_id=7,
        current_user=SimpleNamespace(id=3),
        call_snapshot=_snapshot(),
        cache_root=tmp_path / "private-cache",
        call_id="call-1",
        billing_dedupe_key="tts:2",
        billing_service=billing,
        on_pcmu_chunk=cache_hit_must_not_stream,
        on_pcmu_complete=cache_hit_must_not_stream,
    )

    assert first.text == "Exact audible text."
    assert first.pcmu == second.pcmu == raw_pcmu
    assert first.cache.path == second.cache.path
    assert first.cache.path.suffix == ".mulaw"
    assert "private-cache" in str(first.cache.path)
    assert provider_calls == 1
    assert captured["voice_id"] == "canonical-voice"
    assert captured["model_id"] == "eleven_flash_v2_5"
    assert captured["output_format"] == "ulaw_8000"
    assert stream_events == [
        ("chunk", raw_pcmu[:80]),
        ("chunk", raw_pcmu[80:]),
        ("complete", raw_pcmu),
    ]
    assert [event[0] for event in billing.events] == [
        "reserve",
        "provider_started",
        "settle",
        "cache_hit",
    ]


@pytest.mark.asyncio
async def test_identical_concurrent_fragments_cross_provider_only_once(
    monkeypatch,
    tmp_path,
):
    billing = _BillingService()
    provider_entered = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def fake_key(_voice_id):
        return "test-key"

    async def slow_request(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        await release_provider.wait()
        return b"\xff" * 160

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(speech, "_request_elevenlabs_phone_pcmu", slow_request)

    async def render(dedupe_key):
        return await speech.render_phone_speech(
            text="The same fragment.",
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
            call_id="call-1",
            billing_dedupe_key=dedupe_key,
            billing_service=billing,
        )

    first_task = asyncio.create_task(render("tts:concurrent:1"))
    await asyncio.wait_for(provider_entered.wait(), timeout=2)
    second_task = asyncio.create_task(render("tts:concurrent:2"))
    for _ in range(1_000):
        with speech._PCMU_RENDER_FLIGHTS_GUARD:
            users = [flight.users for flight in speech._PCMU_RENDER_FLIGHTS.values()]
        if users == [2]:
            break
        await asyncio.sleep(0.001)
    else:
        raise AssertionError("second renderer did not join the digest flight")

    release_provider.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first.pcmu == second.pcmu == b"\xff" * 160
    assert provider_calls == 1
    assert [event[0] for event in billing.events] == [
        "reserve",
        "provider_started",
        "settle",
        "cache_hit",
    ]
    assert speech._PCMU_RENDER_FLIGHTS == {}


@pytest.mark.asyncio
async def test_distinct_fragments_are_not_globally_serialized(monkeypatch, tmp_path):
    billing = _BillingService()
    both_providers_entered = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def fake_key(_voice_id):
        return "test-key"

    async def slow_request(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        if provider_calls == 2:
            both_providers_entered.set()
        await release_provider.wait()
        return b"\xff" * 160

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(speech, "_request_elevenlabs_phone_pcmu", slow_request)

    async def render(text, dedupe_key):
        return await speech.render_phone_speech(
            text=text,
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
            call_id="call-1",
            billing_dedupe_key=dedupe_key,
            billing_service=billing,
        )

    tasks = [
        asyncio.create_task(render("First distinct fragment.", "tts:distinct:1")),
        asyncio.create_task(render("Second distinct fragment.", "tts:distinct:2")),
    ]
    await asyncio.wait_for(both_providers_entered.wait(), timeout=2)
    release_provider.set()
    await asyncio.gather(*tasks)

    assert provider_calls == 2
    assert speech._PCMU_RENDER_FLIGHTS == {}


@pytest.mark.asyncio
async def test_elevenlabs_cache_miss_fails_closed_without_billing_context(
    monkeypatch,
    tmp_path,
):
    async def provider_must_not_run(*_args, **_kwargs):
        raise AssertionError("provider must not run without durable billing")

    monkeypatch.setattr(
        speech,
        "_select_elevenlabs_phone_key",
        provider_must_not_run,
    )
    monkeypatch.setattr(
        speech,
        "_request_elevenlabs_phone_pcmu",
        provider_must_not_run,
    )

    with pytest.raises(PhoneSpeechError, match="billing identity"):
        await speech.render_phone_speech(
            text="This must not reach the provider.",
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
        )

    assert speech._PCMU_RENDER_FLIGHTS == {}


@pytest.mark.asyncio
async def test_elevenlabs_renderer_cancellation_marks_ambiguous_and_releases_flight(
    monkeypatch,
    tmp_path,
):
    billing = _BillingService()
    provider_entered = asyncio.Event()

    async def fake_key(_voice_id):
        return "test-key"

    async def never_finishes(**_kwargs):
        provider_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(speech, "_request_elevenlabs_phone_pcmu", never_finishes)

    task = asyncio.create_task(
        speech.render_phone_speech(
            text="Cancel this fragment.",
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
            call_id="call-1",
            billing_dedupe_key="tts:cancel",
            billing_service=billing,
        )
    )
    await asyncio.wait_for(provider_entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [event[0] for event in billing.events] == [
        "reserve",
        "provider_started",
        "ambiguous",
    ]
    assert billing.events[-1][2] == "tts_generation_cancelled"
    assert speech._PCMU_RENDER_FLIGHTS == {}


@pytest.mark.asyncio
async def test_elevenlabs_renderer_marks_provider_failure_ambiguous(
    monkeypatch,
    tmp_path,
):
    billing = _BillingService()

    async def fake_key(_voice_id):
        return "test-key"

    async def failed_request(**_kwargs):
        raise PhoneSpeechError("provider rejected test request")

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(speech, "_request_elevenlabs_phone_pcmu", failed_request)

    with pytest.raises(PhoneSpeechError, match="provider rejected"):
        await speech.render_phone_speech(
            text="This request fails.",
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
            call_id="call-1",
            billing_dedupe_key="tts:failure",
            billing_service=billing,
        )

    assert [event[0] for event in billing.events] == [
        "reserve",
        "provider_started",
        "ambiguous",
    ]
    assert "PhoneSpeechError" in billing.events[-1][2]


@pytest.mark.asyncio
async def test_elevenlabs_renderer_stops_before_provider_when_balance_is_exhausted(
    monkeypatch,
    tmp_path,
):
    billing = _BillingService(exhausted=True)

    async def fake_key(_voice_id):
        return "test-key"

    async def provider_must_not_run(**_kwargs):
        raise AssertionError("provider must not run without a billing reservation")

    monkeypatch.setattr(speech, "_select_elevenlabs_phone_key", fake_key)
    monkeypatch.setattr(
        speech,
        "_request_elevenlabs_phone_pcmu",
        provider_must_not_run,
    )

    with pytest.raises(PhoneSpeechBillingExhausted):
        await speech.render_phone_speech(
            text="No balance.",
            conversation_id=7,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
            cache_root=tmp_path / "private-cache",
            call_id="call-1",
            billing_dedupe_key="tts:exhausted",
            billing_service=billing,
        )

    assert [event[0] for event in billing.events] == ["reserve"]


@pytest.mark.asyncio
async def test_openai_renderer_keeps_existing_tts_and_conversion_path(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / ("a" * 64 + ".opus")
    source.write_bytes(b"source-audio")
    captured = {}

    async def fake_tts(_websocket, data, _user, **kwargs):
        captured["data"] = data
        captured.update(kwargs)
        return str(source), None

    def fake_materialize(_source: Path, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"\xff" * 160)
        return PcmuCacheAsset(
            path=destination,
            byte_length=160,
            duration_ms=20.0,
            sha256="b" * 64,
        )

    monkeypatch.setattr(speech, "handle_tts_request", fake_tts)
    monkeypatch.setattr(speech, "materialize_pcmu_cache", fake_materialize)

    asset = await speech.render_phone_speech(
        text="Existing OpenAI path.",
        conversation_id=7,
        current_user=SimpleNamespace(id=3),
        call_snapshot=_snapshot(provider="openai"),
        cache_root=tmp_path / "private-cache",
    )

    assert asset.pcmu == b"\xff" * 160
    assert captured["resolved_voice_override"].provider == "openai"
    assert captured["tts_profile_override"].model_id == "snapshot-model"
    assert captured["is_whatsapp"] is True


@pytest.mark.asyncio
async def test_renderer_rejects_another_conversations_snapshot():
    with pytest.raises(PhoneSpeechError, match="another conversation"):
        await speech.render_phone_speech(
            text="Hello",
            conversation_id=8,
            current_user=SimpleNamespace(id=3),
            call_snapshot=_snapshot(),
        )
