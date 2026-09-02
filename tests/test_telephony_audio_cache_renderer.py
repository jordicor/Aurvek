import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

import aiosqlite
import pytest

import common
from ai_runtime.voice_resolution import CanonicalVoice
from integrations.telephony.audio_cache_renderer import (
    AurvekTtsBillingAdapter,
    ELEVENLABS_FORCED_ALIGNMENT_URL,
    ELEVENLABS_TTS_WITH_TIMESTAMPS_URL,
    OPENAI_TTS_URL,
    PhoneAudioCacheRenderer,
    PhoneAudioRenderAttemptRepository,
    PhoneAudioRendererError,
    PhoneAudioRendererNeedsAttention,
    ProviderResponse,
    ProviderResponseTooLarge,
    ProviderTransportError,
    _read_bounded_response,
)
from tools.tts_config import TTSProfile


SCHEMA = """
CREATE TABLE PHONE_PROMPT_AUDIO_CACHE (
    cache_key TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE VOICE_CANONICAL_ACTIVATIONS (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL
);
CREATE TABLE PHONE_AUDIO_RENDER_ATTEMPTS (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cache_key TEXT NOT NULL,
    render_fingerprint TEXT NOT NULL,
    billing_user_id INTEGER NOT NULL,
    activation_id TEXT NOT NULL,
    tts_reservation_id TEXT,
    alignment_reservation_id TEXT,
    provider_state TEXT NOT NULL DEFAULT 'pending',
    alignment_state TEXT NOT NULL DEFAULT 'pending',
    needs_attention INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    provider_started_at TEXT,
    provider_succeeded_at TEXT,
    alignment_started_at TEXT,
    alignment_succeeded_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cache_key,render_fingerprint)
);
"""


class FakeTransport:
    def __init__(self, *, json_results=(), multipart_results=()):
        self.json_results = list(json_results)
        self.multipart_results = list(multipart_results)
        self.json_calls = []
        self.multipart_calls = []

    async def post_json(self, url, **kwargs):
        self.json_calls.append((url, kwargs))
        result = self.json_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def post_multipart(self, url, **kwargs):
        self.multipart_calls.append((url, kwargs))
        result = self.multipart_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeBilling:
    def __init__(self):
        self.reserved = []
        self.claimed = []
        self.succeeded = []
        self.settled = []
        self.refunded = []
        self.claim_result = True

    async def reserve_tts(self, **kwargs):
        reservation_id = f"reservation-{len(self.reserved) + 1}"
        self.reserved.append((reservation_id, kwargs))
        return reservation_id

    async def reserve_alignment(self, **kwargs):
        reservation_id = f"reservation-{len(self.reserved) + 1}"
        self.reserved.append((reservation_id, kwargs | {"purpose": "alignment"}))
        return reservation_id

    async def claim(self, reservation_id, *, user_id, purpose):
        self.claimed.append((reservation_id, user_id, purpose))
        return self.claim_result

    async def mark_succeeded(self, reservation_id, *, user_id, purpose):
        self.succeeded.append((reservation_id, user_id, purpose))
        return True

    async def settle(self, reservation_id):
        self.settled.append(reservation_id)
        return True

    async def refund(self, reservation_id):
        self.refunded.append(reservation_id)
        return True


class BlockingRefundBilling(FakeBilling):
    def __init__(self):
        super().__init__()
        self.refund_entered = asyncio.Event()
        self.refund_release = asyncio.Event()

    async def refund(self, reservation_id):
        self.refunded.append(reservation_id)
        self.refund_entered.set()
        await self.refund_release.wait()
        return True


class BlockingTransitionRepository(PhoneAudioRenderAttemptRepository):
    def __init__(self, connection_factory, *, transition):
        super().__init__(connection_factory)
        self.transition = transition
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def provider_succeeded(self, attempt_id):
        if self.transition == "provider":
            self.entered.set()
            await self.release.wait()
        await super().provider_succeeded(attempt_id)

    async def alignment_succeeded(self, attempt_id):
        if self.transition == "alignment":
            self.entered.set()
            await self.release.wait()
        await super().alignment_succeeded(attempt_id)


@pytest.fixture()
def render_db(tmp_path):
    path = tmp_path / "renderer.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO PHONE_PROMPT_AUDIO_CACHE VALUES('cache-1','pending')"
        )
        conn.execute(
            "INSERT INTO VOICE_CANONICAL_ACTIVATIONS VALUES('activation-1','pending')"
        )
        conn.commit()

    @asynccontextmanager
    async def factory():
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    return path, factory


def _voice(provider="elevenlabs"):
    return CanonicalVoice(
        id=11,
        voice_code="canonical-voice",
        name="Canonical",
        tts_service=7,
        service_name=f"TTS-{provider.upper()}",
        provider=provider,
        inherited_default=False,
    )


def _profile():
    return TTSProfile(
        model_id="eleven_multilingual_v2",
        output_format="mp3_44100_128",
        stability=0.42,
        similarity_boost=0.81,
        ws_enabled=False,
        chunk_schedule=[120],
    )


def _fingerprint(text="Hi"):
    return hashlib.sha256(text.encode()).hexdigest()


def _mp3():
    return b"ID3" + b"provider-audio" * 20


def _eleven_success(text="Hi"):
    alignment = {
        "characters": list(text),
        "character_start_times_seconds": [index * 0.1 for index in range(len(text))],
        "character_end_times_seconds": [
            (index + 1) * 0.1 for index in range(len(text))
        ],
    }
    body = json.dumps(
        {
            "audio_base64": base64.b64encode(_mp3()).decode(),
            "alignment": alignment,
            # Deliberately different: original alignment is authoritative.
            "normalized_alignment": {
                "characters": list("NO"),
                "character_start_times_seconds": [0, 0.1],
                "character_end_times_seconds": [0.1, 0.2],
            },
        }
    ).encode()
    return ProviderResponse(200, body, "application/json")


def _forced_success(text="Hi"):
    body = json.dumps(
        {
            "characters": [
                {
                    "text": character,
                    "start": index * 0.1,
                    "end": (index + 1) * 0.1,
                }
                for index, character in enumerate(text)
            ],
            "words": [],
            "loss": 0.01,
        }
    ).encode()
    return ProviderResponse(200, body, "application/json")


def _renderer(render_db, tmp_path, transport, billing):
    _, factory = render_db
    return PhoneAudioCacheRenderer(
        repository=PhoneAudioRenderAttemptRepository(factory),
        transport=transport,
        billing=billing,
        artifact_root=tmp_path / "artifacts",
        elevenlabs_key_getter=lambda: "eleven-secret",
        openai_key_getter=lambda: "openai-secret",
        mp3_duration_probe=lambda _audio: 1.5,
    )


def _renderer_with_repository(
    repository, tmp_path, transport, billing
):
    return PhoneAudioCacheRenderer(
        repository=repository,
        transport=transport,
        billing=billing,
        artifact_root=tmp_path / "artifacts",
        elevenlabs_key_getter=lambda: "eleven-secret",
        openai_key_getter=lambda: "openai-secret",
        mp3_duration_probe=lambda _audio: 1.5,
    )


def _insert_second_revision(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO PHONE_PROMPT_AUDIO_CACHE VALUES('cache-2','pending')"
        )
        conn.execute(
            "INSERT INTO VOICE_CANONICAL_ACTIVATIONS VALUES('activation-2','pending')"
        )
        conn.commit()


async def _render_second_revision(renderer, *, provider="elevenlabs"):
    return await renderer(
        literal_text="Hi",
        voice=_voice(provider),
        profile=_profile(),
        billing_user_id=9,
        activation_id="activation-2",
        cache_key="cache-2",
        render_fingerprint=_fingerprint(),
    )


async def _render(renderer, *, provider="elevenlabs", text="Hi"):
    return await renderer(
        literal_text=text,
        voice=_voice(provider),
        profile=_profile(),
        billing_user_id=9,
        activation_id="activation-1",
        cache_key="cache-1",
        render_fingerprint=_fingerprint(text),
    )


def _attempt(path: Path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM PHONE_AUDIO_RENDER_ATTEMPTS").fetchone()
        return dict(row)


@pytest.mark.asyncio
async def test_elevenlabs_uses_timestamped_mp3_original_alignment_and_bills_once(
    render_db, tmp_path
):
    transport = FakeTransport(json_results=[_eleven_success()])
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    rendered = await _render(renderer)

    assert rendered.path.read_bytes() == _mp3()
    assert rendered.alignment.text == "Hi"
    assert rendered.alignment.character_start_ms == (0, 100)
    assert rendered.alignment.character_end_ms == (100, 200)
    url, request = transport.json_calls[0]
    assert url == (
        ELEVENLABS_TTS_WITH_TIMESTAMPS_URL.format(
            voice_id="canonical-voice"
        )
        + "?output_format=mp3_44100_128"
    )
    assert request["payload"] == {
        "text": "Hi",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.42, "similarity_boost": 0.81},
    }
    assert request["headers"]["xi-api-key"] == "eleven-secret"
    assert billing.reserved == [
        (
            "reservation-1",
            {"user_id": 9, "provider": "elevenlabs", "characters": 2},
        )
    ]
    assert billing.claimed == [("reservation-1", 9, "tts")]
    assert billing.succeeded == [("reservation-1", 9, "tts")]
    assert billing.settled == ["reservation-1"]
    assert billing.refunded == []
    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["alignment_state"] == "not_required"
    assert attempt["completed_at"] is not None


@pytest.mark.asyncio
async def test_elevenlabs_key_selection_receives_canonical_voice_id(
    render_db, tmp_path
):
    _, factory = render_db
    requested = []

    def key_for_voice(voice_id):
        requested.append(voice_id)
        return "eleven-secret"

    renderer = PhoneAudioCacheRenderer(
        repository=PhoneAudioRenderAttemptRepository(factory),
        transport=FakeTransport(json_results=[_eleven_success()]),
        billing=FakeBilling(),
        artifact_root=tmp_path / "artifacts",
        elevenlabs_key_getter=key_for_voice,
        openai_key_getter=lambda: "openai-secret",
    )

    await _render(renderer)

    assert requested == ["canonical-voice"]


@pytest.mark.asyncio
async def test_completed_retry_reuses_fingerprint_without_provider_or_billing(
    render_db, tmp_path
):
    first_transport = FakeTransport(json_results=[_eleven_success()])
    first_billing = FakeBilling()
    first = _renderer(render_db, tmp_path, first_transport, first_billing)
    initial = await _render(first)

    retry_transport = FakeTransport()
    retry_billing = FakeBilling()
    retry = _renderer(render_db, tmp_path, retry_transport, retry_billing)
    repeated = await _render(retry)

    assert repeated.path == initial.path
    assert retry_transport.json_calls == []
    assert retry_billing.reserved == []
    assert retry_billing.claimed == []
    assert retry_billing.settled == []


@pytest.mark.asyncio
async def test_new_revision_reuses_completed_fingerprint_without_double_charge(
    render_db, tmp_path
):
    first_transport = FakeTransport(json_results=[_eleven_success()])
    first_billing = FakeBilling()
    await _render(_renderer(render_db, tmp_path, first_transport, first_billing))
    with sqlite3.connect(render_db[0]) as conn:
        conn.execute(
            "INSERT INTO PHONE_PROMPT_AUDIO_CACHE VALUES('cache-2','pending')"
        )
        conn.execute(
            "INSERT INTO VOICE_CANONICAL_ACTIVATIONS VALUES('activation-2','pending')"
        )
        conn.commit()

    second_transport = FakeTransport()
    second_billing = FakeBilling()
    second = _renderer(render_db, tmp_path, second_transport, second_billing)
    rendered = await second(
        literal_text="Hi",
        voice=_voice(),
        profile=_profile(),
        billing_user_id=9,
        activation_id="activation-2",
        cache_key="cache-2",
        render_fingerprint=_fingerprint(),
    )

    assert rendered.path.read_bytes() == _mp3()
    assert second_transport.json_calls == []
    assert second_billing.reserved == []
    with sqlite3.connect(render_db[0]) as conn:
        rows = conn.execute(
            "SELECT cache_key,provider_state,alignment_state,completed_at "
            "FROM PHONE_AUDIO_RENDER_ATTEMPTS ORDER BY id"
        ).fetchall()
    assert rows[0][0:3] == ("cache-1", "succeeded", "not_required")
    assert rows[1][0:3] == ("cache-2", "succeeded", "not_required")
    assert all(row[3] is not None for row in rows)


@pytest.mark.asyncio
async def test_timeout_is_ambiguous_and_retry_is_fenced(render_db, tmp_path):
    transport = FakeTransport(
        json_results=[ProviderTransportError("timeout")]
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(PhoneAudioRendererNeedsAttention, match="ambiguous"):
        await _render(renderer)

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "ambiguous"
    assert attempt["needs_attention"] == 1
    assert billing.settled == []
    assert billing.refunded == []

    with pytest.raises(PhoneAudioRendererNeedsAttention, match="replayed"):
        await _render(renderer)
    assert len(transport.json_calls) == 1
    assert len(billing.reserved) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [422, 503])
async def test_definitive_http_error_refunds_and_records_failed(
    render_db, tmp_path, status
):
    transport = FakeTransport(
        json_results=[ProviderResponse(status, b'{"detail":"known response"}')]
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(PhoneAudioRendererError, match=str(status)):
        await _render(renderer)

    assert billing.refunded == ["reservation-1"]
    assert billing.succeeded == []
    assert _attempt(render_db[0])["provider_state"] == "failed"


@pytest.mark.asyncio
async def test_cancel_during_known_response_refund_still_records_failed(
    render_db, tmp_path
):
    billing = BlockingRefundBilling()
    transport = FakeTransport(
        json_results=[ProviderResponse(503, b'{"detail":"known failure"}')]
    )
    task = asyncio.create_task(
        _render(_renderer(render_db, tmp_path, transport, billing))
    )
    await billing.refund_entered.wait()
    task.cancel()
    billing.refund_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "failed"
    assert attempt["needs_attention"] == 0
    assert billing.refunded == ["reservation-1"]
    with pytest.raises(PhoneAudioRendererError, match="already failed"):
        await _render(_renderer(render_db, tmp_path, FakeTransport(), FakeBilling()))
    assert len(transport.json_calls) == 1


@pytest.mark.asyncio
async def test_cancel_during_provider_success_transition_is_durable_and_fenced(
    render_db, tmp_path
):
    repository = BlockingTransitionRepository(
        render_db[1], transition="provider"
    )
    billing = FakeBilling()
    transport = FakeTransport(json_results=[_eleven_success()])
    task = asyncio.create_task(
        _render(
            _renderer_with_repository(
                repository, tmp_path, transport, billing
            )
        )
    )
    await repository.entered.wait()
    task.cancel()
    repository.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["needs_attention"] == 1
    assert billing.succeeded == [("reservation-1", 9, "tts")]
    assert billing.settled == ["reservation-1"]

    _insert_second_revision(render_db[0])
    retry_transport = FakeTransport()
    retry_billing = FakeBilling()
    with pytest.raises(PhoneAudioRendererNeedsAttention):
        await _render_second_revision(
            _renderer(render_db, tmp_path, retry_transport, retry_billing)
        )
    assert retry_transport.json_calls == []
    assert retry_billing.reserved == []


@pytest.mark.asyncio
async def test_cancel_during_alignment_success_transition_is_durable_and_fenced(
    render_db, tmp_path
):
    repository = BlockingTransitionRepository(
        render_db[1], transition="alignment"
    )
    billing = FakeBilling()
    transport = FakeTransport(
        json_results=[ProviderResponse(200, _mp3(), "audio/mpeg")],
        multipart_results=[_forced_success()],
    )
    task = asyncio.create_task(
        _render(
            _renderer_with_repository(
                repository, tmp_path, transport, billing
            ),
            provider="openai",
        )
    )
    await repository.entered.wait()
    task.cancel()
    repository.release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["alignment_state"] == "succeeded"
    assert attempt["needs_attention"] == 1
    assert billing.succeeded == [
        ("reservation-1", 9, "tts"),
        ("reservation-2", 9, "stt"),
    ]
    assert billing.settled == ["reservation-1", "reservation-2"]

    _insert_second_revision(render_db[0])
    retry_transport = FakeTransport()
    retry_billing = FakeBilling()
    with pytest.raises(PhoneAudioRendererNeedsAttention):
        await _render_second_revision(
            _renderer(render_db, tmp_path, retry_transport, retry_billing),
            provider="openai",
        )
    assert retry_transport.json_calls == []
    assert retry_transport.multipart_calls == []
    assert retry_billing.reserved == []


@pytest.mark.asyncio
async def test_invalid_alignment_settles_billable_provider_once_but_no_artifact(
    render_db, tmp_path
):
    payload = json.loads(_eleven_success().body)
    payload["alignment"]["characters"] = ["N", "O"]
    transport = FakeTransport(
        json_results=[ProviderResponse(200, json.dumps(payload).encode())]
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(PhoneAudioRendererError, match="unusable"):
        await _render(renderer)

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["alignment_state"] == "failed"
    assert attempt["needs_attention"] == 1
    assert billing.succeeded == [("reservation-1", 9, "tts")]
    assert billing.settled == ["reservation-1"]
    assert not list((tmp_path / "artifacts").rglob("*.ready.json"))


@pytest.mark.asyncio
async def test_invalid_elevenlabs_alignment_fences_equivalent_new_revision(
    render_db, tmp_path
):
    payload = json.loads(_eleven_success().body)
    payload["alignment"]["characters"] = ["N", "O"]
    first_transport = FakeTransport(
        json_results=[ProviderResponse(200, json.dumps(payload).encode())]
    )
    first_billing = FakeBilling()
    with pytest.raises(PhoneAudioRendererError, match="unusable"):
        await _render(
            _renderer(render_db, tmp_path, first_transport, first_billing)
        )

    with sqlite3.connect(render_db[0]) as conn:
        conn.execute(
            "INSERT INTO PHONE_PROMPT_AUDIO_CACHE VALUES('cache-2','pending')"
        )
        conn.execute(
            "INSERT INTO VOICE_CANONICAL_ACTIVATIONS VALUES('activation-2','pending')"
        )
        conn.commit()

    retry_transport = FakeTransport()
    retry_billing = FakeBilling()
    retry = _renderer(render_db, tmp_path, retry_transport, retry_billing)
    with pytest.raises(PhoneAudioRendererNeedsAttention, match="replayed safely"):
        await retry(
            literal_text="Hi",
            voice=_voice(),
            profile=_profile(),
            billing_user_id=9,
            activation_id="activation-2",
            cache_key="cache-2",
            render_fingerprint=_fingerprint(),
        )

    assert retry_transport.json_calls == []
    assert retry_billing.reserved == []
    with sqlite3.connect(render_db[0]) as conn:
        attempts = conn.execute(
            "SELECT cache_key,needs_attention,completed_at "
            "FROM PHONE_AUDIO_RENDER_ATTEMPTS ORDER BY id"
        ).fetchall()
    assert attempts == [("cache-1", 1, None)]


@pytest.mark.asyncio
async def test_openai_mp3_then_forced_alignment_preserves_voice_and_bills_each_work(
    render_db, tmp_path
):
    transport = FakeTransport(
        json_results=[ProviderResponse(200, _mp3(), "audio/mpeg")],
        multipart_results=[_forced_success()],
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    rendered = await _render(renderer, provider="openai")

    assert rendered.path.read_bytes() == _mp3()
    openai_url, openai_request = transport.json_calls[0]
    assert openai_url == OPENAI_TTS_URL
    assert openai_request["payload"] == {
        "model": "tts-1",
        "input": "Hi",
        "voice": "canonical-voice",
        "response_format": "mp3",
    }
    assert openai_request["headers"]["Authorization"] == "Bearer openai-secret"
    alignment_url, alignment_request = transport.multipart_calls[0]
    assert alignment_url == ELEVENLABS_FORCED_ALIGNMENT_URL
    assert alignment_request["fields"] == {"text": "Hi"}
    assert alignment_request["file_bytes"] == _mp3()
    assert billing.reserved == [
        (
            "reservation-1",
            {"user_id": 9, "provider": "openai", "characters": 2},
        ),
        (
            "reservation-2",
            {"user_id": 9, "duration_seconds": 1.5, "purpose": "alignment"},
        ),
    ]
    assert billing.claimed == [
        ("reservation-1", 9, "tts"),
        ("reservation-2", 9, "stt"),
    ]
    assert billing.succeeded == [
        ("reservation-1", 9, "tts"),
        ("reservation-2", 9, "stt"),
    ]
    assert billing.settled == ["reservation-1", "reservation-2"]
    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["alignment_state"] == "succeeded"
    assert attempt["completed_at"] is not None


@pytest.mark.asyncio
async def test_openai_fails_closed_before_billing_when_aligner_key_is_missing(
    render_db, tmp_path
):
    _, factory = render_db
    transport = FakeTransport()
    billing = FakeBilling()
    renderer = PhoneAudioCacheRenderer(
        repository=PhoneAudioRenderAttemptRepository(factory),
        transport=transport,
        billing=billing,
        artifact_root=tmp_path / "artifacts",
        elevenlabs_key_getter=lambda: None,
        openai_key_getter=lambda: "openai-secret",
    )

    with pytest.raises(PhoneAudioRendererError, match="ElevenLabs API key"):
        await _render(renderer, provider="openai")

    assert billing.reserved == []
    assert transport.json_calls == []
    assert _attempt(render_db[0])["provider_state"] == "failed"


@pytest.mark.asyncio
async def test_openai_alignment_timeout_never_repeats_openai_tts(render_db, tmp_path):
    transport = FakeTransport(
        json_results=[ProviderResponse(200, _mp3(), "audio/mpeg")],
        multipart_results=[ProviderTransportError("alignment timeout")],
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(PhoneAudioRendererNeedsAttention, match="alignment"):
        await _render(renderer, provider="openai")

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "succeeded"
    assert attempt["alignment_state"] == "ambiguous"
    assert attempt["needs_attention"] == 1
    assert billing.settled == ["reservation-1"]
    assert billing.refunded == []

    with pytest.raises(PhoneAudioRendererNeedsAttention):
        await _render(renderer, provider="openai")
    assert len(transport.json_calls) == 1
    assert len(transport.multipart_calls) == 1
    assert len(billing.reserved) == 2


@pytest.mark.asyncio
async def test_new_revision_reuses_paid_openai_mp3_after_known_alignment_failure(
    render_db, tmp_path
):
    first_transport = FakeTransport(
        json_results=[ProviderResponse(200, _mp3(), "audio/mpeg")],
        multipart_results=[ProviderResponse(422, b'{"detail":"bad text"}')],
    )
    first_billing = FakeBilling()
    first = _renderer(render_db, tmp_path, first_transport, first_billing)
    with pytest.raises(PhoneAudioRendererError, match="alignment failed"):
        await _render(first, provider="openai")

    assert first_billing.settled == ["reservation-1"]
    assert first_billing.refunded == ["reservation-2"]
    with sqlite3.connect(render_db[0]) as conn:
        conn.execute(
            "INSERT INTO PHONE_PROMPT_AUDIO_CACHE VALUES('cache-2','pending')"
        )
        conn.execute(
            "INSERT INTO VOICE_CANONICAL_ACTIVATIONS VALUES('activation-2','pending')"
        )
        conn.commit()

    retry_transport = FakeTransport(multipart_results=[_forced_success()])
    retry_billing = FakeBilling()
    retry = _renderer(render_db, tmp_path, retry_transport, retry_billing)
    rendered = await retry(
        literal_text="Hi",
        voice=_voice("openai"),
        profile=_profile(),
        billing_user_id=9,
        activation_id="activation-2",
        cache_key="cache-2",
        render_fingerprint=_fingerprint(),
    )

    assert rendered.path.read_bytes() == _mp3()
    assert retry_transport.json_calls == []
    assert len(retry_transport.multipart_calls) == 1
    assert retry_billing.reserved == [
        (
            "reservation-1",
            {"user_id": 9, "duration_seconds": 1.5, "purpose": "alignment"},
        )
    ]
    assert retry_billing.claimed == [("reservation-1", 9, "stt")]
    assert retry_billing.settled == ["reservation-1"]


@pytest.mark.asyncio
async def test_oversized_or_cancelled_provider_work_leaves_no_artifact(
    render_db, tmp_path
):
    transport = FakeTransport(
        json_results=[ProviderResponseTooLarge("too large")]
    )
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(PhoneAudioRendererNeedsAttention):
        await _render(renderer)

    assert not list((tmp_path / "artifacts").rglob("*.tmp"))
    assert not list((tmp_path / "artifacts").rglob("*.ready.json"))


@pytest.mark.asyncio
async def test_cancellation_is_fenced_and_has_no_temporary_files(render_db, tmp_path):
    transport = FakeTransport(json_results=[asyncio.CancelledError()])
    billing = FakeBilling()
    renderer = _renderer(render_db, tmp_path, transport, billing)

    with pytest.raises(asyncio.CancelledError):
        await _render(renderer)

    attempt = _attempt(render_db[0])
    assert attempt["provider_state"] == "ambiguous"
    assert attempt["needs_attention"] == 1
    assert not list((tmp_path / "artifacts").rglob("*.tmp"))


@pytest.mark.asyncio
async def test_cancel_during_failed_publication_propagates_failure_and_cleans_finals(
    render_db, tmp_path, monkeypatch
):
    from integrations.telephony import audio_cache_renderer as renderer_module

    entered = threading.Event()
    release = threading.Event()
    original_replace = os.replace

    def controlled_replace(source, destination):
        if str(destination).endswith(".alignment.json"):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("publication test did not release writer")
            raise OSError("controlled alignment publication failure")
        return original_replace(source, destination)

    monkeypatch.setattr(renderer_module.os, "replace", controlled_replace)
    renderer = _renderer(
        render_db,
        tmp_path,
        FakeTransport(json_results=[_eleven_success()]),
        FakeBilling(),
    )
    task = asyncio.create_task(_render(renderer))
    assert await asyncio.to_thread(entered.wait, 2)
    task.cancel()
    release.set()

    with pytest.raises(PhoneAudioRendererError, match="unusable"):
        await task

    attempt = _attempt(render_db[0])
    assert attempt["completed_at"] is None
    assert attempt["needs_attention"] == 1
    artifact_root = tmp_path / "artifacts"
    assert not list(artifact_root.rglob("*.mp3"))
    assert not list(artifact_root.rglob("*.alignment.json"))
    assert not list(artifact_root.rglob("*.ready.json"))
    assert not list(artifact_root.rglob("*.tmp"))


@pytest.mark.asyncio
async def test_alignment_reservation_is_provider_specific_stt_duration_billing(
    monkeypatch,
):
    captured = []

    async def reserve_fixed_usage(**kwargs):
        captured.append(kwargs)
        return "alignment-reservation"

    monkeypatch.setattr(
        "billing.usage_reservations.reserve_fixed_usage",
        reserve_fixed_usage,
    )
    monkeypatch.setattr(
        common.Cost,
        "STT_PROVIDER_SERVICES",
        {
            "elevenlabs": {"cost_per_minute": 0.006, "service_id": 51},
            "deepgram": {"cost_per_minute": 0.004, "service_id": 52},
        },
    )

    reservation = await AurvekTtsBillingAdapter().reserve_alignment(
        user_id=9,
        duration_seconds=90.0,
    )

    assert reservation == "alignment-reservation"
    assert captured == [
        {
            "user_id": 9,
            "purpose": "stt",
            "amount": pytest.approx(0.009),
            "service_id": 51,
            "usage_quantity": pytest.approx(1.5),
        }
    ]


@pytest.mark.asyncio
async def test_http_response_reader_enforces_stream_limit_without_content_length():
    class Content:
        async def iter_chunked(self, _size):
            yield b"1234"
            yield b"5678"

    class Response:
        headers = {}
        content = Content()

    with pytest.raises(ProviderResponseTooLarge):
        await _read_bounded_response(Response(), 7)
