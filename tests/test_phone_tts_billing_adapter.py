from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from ai_runtime.voice_resolution import CanonicalVoice
from tools import tts
from tools.tts_config import TTSProfile


def _voice() -> CanonicalVoice:
    return CanonicalVoice(
        id=1,
        voice_code="alloy",
        name="Alloy",
        tts_service=1,
        service_name="TTS-OPENAI",
        provider="openai",
        inherited_default=False,
    )


def _elevenlabs_voice() -> CanonicalVoice:
    return CanonicalVoice(
        id=2,
        voice_code="account-scoped-voice",
        name="Account scoped",
        tts_service=2,
        service_name="TTS-ELEVENLABS",
        provider="elevenlabs",
        inherited_default=False,
    )


class RecordingAdapter:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.token = object()

    async def cache_hit(self, *, provider, characters, cache_key):
        self.events.append(("cache_hit", provider, characters, cache_key))

    async def reserve(self, *, provider, characters):
        self.events.append(("reserve", provider, characters))
        return self.token

    async def provider_started(self, token):
        assert token is self.token
        self.events.append(("provider_started",))
        return True

    async def settle(self, token):
        assert token is self.token
        self.events.append(("settle",))

    async def failed(self, token, *, provider_started, reason):
        assert token is self.token
        self.events.append(("failed", provider_started, reason))


def _patch_renderer(monkeypatch, tmp_path, generator_factory) -> str:
    destination = tmp_path / "rendered.opus"

    async def no_legacy_billing(*_args, **_kwargs):
        raise AssertionError("telephone TTS must not use legacy direct billing")

    async def fragments(text):
        return [text]

    monkeypatch.setattr(tts, "has_sufficient_balance", no_legacy_billing)
    monkeypatch.setattr(tts, "cost_tts", no_legacy_billing)
    monkeypatch.setattr(tts, "refund_tts", no_legacy_billing)
    monkeypatch.setattr(tts, "insert_tts_break", fragments)
    monkeypatch.setattr(tts, "get_tts_generator_for_voice", generator_factory)
    monkeypatch.setattr(tts, "format_to_pydub", lambda _format: "mp3")
    monkeypatch.setattr(tts, "get_file_path", lambda _digest: (str(tmp_path), str(destination)))
    monkeypatch.setattr(tts, "_decode_and_export_audio_chunks", lambda *_args: b"opus")
    monkeypatch.setattr(tts, "_maybe_cleanup_cache", lambda: None)
    return str(destination)


@pytest.mark.asyncio
async def test_phone_adapter_owns_successful_tts_charge_without_double_billing(
    tmp_path, monkeypatch
) -> None:
    async def generated():
        yield b"provider-audio"

    destination = _patch_renderer(
        monkeypatch,
        tmp_path,
        lambda *_args, **_kwargs: generated(),
    )
    adapter = RecordingAdapter()

    result = await tts.handle_tts_request(
        None,
        {"text": "hello", "author": "bot", "conversationId": 7},
        SimpleNamespace(id=1),
        is_whatsapp=True,
        tts_context="external",
        resolved_voice_override=_voice(),
        tts_profile_override=TTSProfile("model", "mp3", 0.4, 0.8),
        billing_adapter=adapter,
    )

    assert result == (destination, None)
    assert adapter.events == [
        ("reserve", "openai", 5),
        ("provider_started",),
        ("settle",),
    ]


@pytest.mark.asyncio
async def test_missing_compatible_elevenlabs_key_fails_before_billing_claim(
    tmp_path, monkeypatch
) -> None:
    async def no_compatible_key(voice_id):
        assert voice_id == "account-scoped-voice"
        return None

    def generator_must_not_start(*_args, **_kwargs):
        raise AssertionError("provider generator must not start without a key")

    _patch_renderer(monkeypatch, tmp_path, generator_must_not_start)
    monkeypatch.setattr(tts, "_select_elevenlabs_key", no_compatible_key)
    adapter = RecordingAdapter()

    result = await tts.handle_tts_request(
        None,
        {"text": "hello", "author": "bot", "conversationId": 7},
        SimpleNamespace(id=1),
        is_whatsapp=True,
        tts_context="external",
        resolved_voice_override=_elevenlabs_voice(),
        tts_profile_override=TTSProfile("model", "mp3", 0.4, 0.8),
        billing_adapter=adapter,
    )

    assert result == (
        None,
        "No valid ElevenLabs API key is available for the selected voice.",
    )
    assert adapter.events == []


@pytest.mark.asyncio
async def test_phone_adapter_marks_partial_tts_cancellation_ambiguous(
    tmp_path, monkeypatch
) -> None:
    provider_entered = asyncio.Event()

    async def generated():
        yield b"partial"
        provider_entered.set()
        await asyncio.Event().wait()

    _patch_renderer(
        monkeypatch,
        tmp_path,
        lambda *_args, **_kwargs: generated(),
    )
    adapter = RecordingAdapter()
    task = asyncio.create_task(
        tts.handle_tts_request(
            None,
            {"text": "hello", "author": "bot", "conversationId": 7},
            SimpleNamespace(id=1),
            is_whatsapp=True,
            tts_context="external",
            resolved_voice_override=_voice(),
            tts_profile_override=TTSProfile("model", "mp3", 0.4, 0.8),
            billing_adapter=adapter,
        )
    )
    await asyncio.wait_for(provider_entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert adapter.events == [
        ("reserve", "openai", 5),
        ("provider_started",),
        ("failed", True, "tts_generation_cancelled"),
    ]


@pytest.mark.asyncio
async def test_concurrent_same_tts_claim_crosses_provider_once(
    tmp_path, monkeypatch
) -> None:
    provider_iterations = 0

    async def generated():
        nonlocal provider_iterations
        provider_iterations += 1
        yield b"provider-audio"

    _patch_renderer(
        monkeypatch,
        tmp_path,
        lambda *_args, **_kwargs: generated(),
    )

    class ExclusiveAdapter:
        def __init__(self):
            self.token = object()
            self.reservations = 0
            self.both_reserved = asyncio.Event()
            self.claim_lock = asyncio.Lock()
            self.claimed = False

        async def cache_hit(self, **_values):
            raise AssertionError("both requests must start from a cache miss")

        async def reserve(self, **_values):
            self.reservations += 1
            if self.reservations == 2:
                self.both_reserved.set()
            await self.both_reserved.wait()
            return self.token

        async def provider_started(self, token):
            assert token is self.token
            async with self.claim_lock:
                if self.claimed:
                    return False
                self.claimed = True
                return True

        async def settle(self, token):
            assert token is self.token

        async def failed(self, token, **_values):
            assert token is self.token

    adapter = ExclusiveAdapter()

    async def render():
        return await tts.handle_tts_request(
            None,
            {"text": "hello", "author": "bot", "conversationId": 7},
            SimpleNamespace(id=1),
            is_whatsapp=True,
            tts_context="external",
            resolved_voice_override=_voice(),
            tts_profile_override=TTSProfile("model", "mp3", 0.4, 0.8),
            billing_adapter=adapter,
        )

    results = await asyncio.gather(render(), render())

    assert provider_iterations == 1
    assert sum(result[0] is not None for result in results) == 1
    assert sum(result[1] is not None for result in results) == 1
