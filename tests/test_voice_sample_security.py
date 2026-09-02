import hashlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app as app_module
from ai_runtime.voice_resolution import CanonicalVoiceResolutionError
from tools.tts import PreviewVoice


class FakeUser:
    def __init__(self, user_id: int, *, admin: bool) -> None:
        self.id = user_id
        self._admin = admin

    @property
    async def is_admin(self):
        return self._admin


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/voice-sample/test",
            "query_string": b"",
            "headers": [],
            "client": ("203.0.113.9", 1234),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


@pytest.mark.asyncio
async def test_voice_sample_rejects_uncatalogued_voice_for_normal_user(monkeypatch):
    monkeypatch.setattr(
        app_module,
        "resolve_playback_voice",
        AsyncMock(
            side_effect=CanonicalVoiceResolutionError(
                "voice_not_catalogued", "Voice is not catalogued."
            )
        ),
    )
    generate = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(app_module, "handle_tts_request", generate)

    with pytest.raises(HTTPException) as exc:
        await app_module.get_voice_sample(
            _request(),
            "external-id",
            category=0,
            provider=None,
            current_user=FakeUser(7, admin=False),
        )

    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == "voice_not_catalogued"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_external_preview_uses_provider_qualified_safe_filename(
    tmp_path, monkeypatch
):
    voice = PreviewVoice("../../external-id", "elevenlabs")
    resolver = AsyncMock(return_value=voice)
    monkeypatch.setattr(app_module, "resolve_playback_voice", resolver)
    monkeypatch.setattr(app_module, "VOICE_SAMPLES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(app_module, "check_rate_limits", lambda *_a, **_k: None)

    generated = tmp_path / "generated.opus"

    async def generate(*_args, **kwargs):
        generated.write_bytes(b"audio")
        assert kwargs["resolved_sample_voice"] is voice
        return str(generated), None

    monkeypatch.setattr(app_module, "handle_tts_request", generate)

    response = await app_module.get_voice_sample(
        _request(),
        voice.voice_code,
        category=3,
        provider="elevenlabs",
        current_user=FakeUser(7, admin=True),
    )

    digest = hashlib.sha256(
        f"{voice.provider}:{voice.voice_code}".encode("utf-8")
    ).hexdigest()
    assert Path(response.path).name == f"{digest}_sample-3.opus"
    assert "external-id" not in str(response.path)
    resolver.assert_awaited_once_with(
        voice.voice_code,
        allow_uncatalogued_preview=True,
        preview_provider="elevenlabs",
    )


@pytest.mark.asyncio
async def test_voice_sample_cache_miss_is_rate_limited_before_provider(monkeypatch, tmp_path):
    voice = PreviewVoice("external-id", "elevenlabs")
    monkeypatch.setattr(app_module, "resolve_playback_voice", AsyncMock(return_value=voice))
    monkeypatch.setattr(app_module, "VOICE_SAMPLES_DIR", str(tmp_path / "samples"))
    monkeypatch.setattr(
        app_module,
        "check_rate_limits",
        lambda *_a, **_k: {
            "message": "Too many attempts.",
            "retry_after_seconds": 12,
        },
    )
    generate = AsyncMock(side_effect=AssertionError("provider must not be called"))
    monkeypatch.setattr(app_module, "handle_tts_request", generate)

    with pytest.raises(HTTPException) as exc:
        await app_module.get_voice_sample(
            _request(),
            voice.voice_code,
            category=0,
            provider="elevenlabs",
            current_user=FakeUser(7, admin=True),
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "12"
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_tts_billing_initialization_is_not_skipped_in_redis_mode(monkeypatch):
    sentinel = RuntimeError("stop after billing init")
    initialize = AsyncMock(side_effect=sentinel)
    monkeypatch.setattr(app_module, "_initialize_tts_billing", initialize)
    monkeypatch.setenv("REDIS_IMG_TOKEN", "1")

    with pytest.raises(RuntimeError, match="stop after billing init"):
        async with app_module.lifespan(app_module.app):
            pass

    initialize.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_missing_optional_tts_provider_is_visible_but_does_not_abort(monkeypatch):
    async def initialize():
        app_module.Cost.TTS_PROVIDER_SERVICES = {
            "elevenlabs": {"cost_per_character": 0.01, "service_id": 1},
            "openai": {"cost_per_character": 0.002, "service_id": None},
        }

    monkeypatch.setattr(app_module.Cost, "initialize", initialize)
    monkeypatch.setattr(app_module, "TTS_BILLING_MISSING_PROVIDERS", ())

    await app_module._initialize_tts_billing()

    assert app_module.TTS_BILLING_MISSING_PROVIDERS == ("openai",)
