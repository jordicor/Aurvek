import pytest
from unittest.mock import AsyncMock

from ai_runtime.voice_resolution import CanonicalVoice
from tools import tts as tts_module
from tools.tts_config import TTSProfile


def _voice(provider: str, voice_code: str) -> CanonicalVoice:
    return CanonicalVoice(
        id=1,
        voice_code=voice_code,
        name=voice_code,
        tts_service=1,
        service_name=f"TTS-{provider.upper()}",
        provider=provider,
        inherited_default=False,
    )


def test_openai_voice_selects_openai_despite_global_elevenlabs(monkeypatch):
    selected = object()
    monkeypatch.setattr(tts_module, "tts_engine", "elevenlabs")
    monkeypatch.setattr(
        tts_module,
        "openai_generator",
        lambda voice_id, chunks: selected,
    )

    result = tts_module.get_tts_generator_for_voice(
        _voice("openai", "alloy"), ["hello"]
    )

    assert result is selected


def test_elevenlabs_voice_selects_elevenlabs_despite_global_openai(monkeypatch):
    selected = object()
    monkeypatch.setattr(tts_module, "tts_engine", "openai")
    monkeypatch.setattr(
        tts_module,
        "_elevenlabs_http_generator",
        lambda voice_id, chunks, **kwargs: selected,
    )

    result = tts_module.get_tts_generator_for_voice(
        _voice("elevenlabs", "el-voice"), ["hello"]
    )

    assert result is selected


def test_tts_cache_identity_includes_provider():
    profile = TTSProfile("model", "mp3_44100_128", 0.45, 0.89, True, [120])

    openai_digest = tts_module.get_tts_cache_digest(
        "same text", _voice("openai", "shared-id"), profile
    )
    elevenlabs_digest = tts_module.get_tts_cache_digest(
        "same text", _voice("elevenlabs", "shared-id"), profile
    )

    assert openai_digest != elevenlabs_digest


def test_tts_cache_identity_changes_with_audible_profile_settings():
    voice = _voice("elevenlabs", "voice")
    base = TTSProfile("model", "mp3_44100_128", 0.45, 0.89, True, [120])
    variants = [
        TTSProfile("model", "mp3_44100_128", 0.55, 0.89, True, [120]),
        TTSProfile("model", "mp3_44100_128", 0.45, 0.79, True, [120]),
        TTSProfile("model", "mp3_44100_128", 0.45, 0.89, True, [160]),
        TTSProfile("model", "mp3_44100_128", 0.45, 0.89, False, [120]),
    ]

    base_digest = tts_module.get_tts_cache_digest("text", voice, base)

    assert all(
        tts_module.get_tts_cache_digest("text", voice, profile) != base_digest
        for profile in variants
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "rate"),
    [("elevenlabs", 0.01), ("openai", 0.002)],
)
async def test_handle_prechecks_balance_with_resolved_provider_rate(
    monkeypatch, provider, rate
):
    requested_amounts = []

    async def get_voice(_conversation_id, _current_user):
        return _voice(provider, f"{provider}-voice")

    async def has_balance(_user_id, amount):
        requested_amounts.append(amount)
        return False

    monkeypatch.setattr(tts_module, "get_voice_from_conversation", get_voice)
    monkeypatch.setattr(tts_module, "has_sufficient_balance", has_balance)
    monkeypatch.setattr(
        tts_module.Cost,
        "TTS_PROVIDER_SERVICES",
        {
            "elevenlabs": {"cost_per_character": 0.01, "service_id": 11},
            "openai": {"cost_per_character": 0.002, "service_id": 22},
        },
    )

    result = await tts_module.handle_tts_request(
        websocket=None,
        data={"text": "hello", "author": "bot", "conversationId": 7},
        current_user=type("User", (), {"id": 1})(),
        is_whatsapp=True,
    )

    assert result == (None, "insufficient-balance")
    assert requested_amounts == [pytest.approx(5 * rate)]


@pytest.mark.asyncio
async def test_call_snapshot_voice_bypasses_live_conversation_resolution_but_bills_normally(
    monkeypatch,
):
    requested_amounts = []

    async def must_not_resolve(*_args, **_kwargs):
        raise AssertionError("live voice resolution must not replace a call snapshot")

    async def has_balance(_user_id, amount):
        requested_amounts.append(amount)
        return False

    monkeypatch.setattr(tts_module, "get_voice_from_conversation", must_not_resolve)
    monkeypatch.setattr(tts_module, "has_sufficient_balance", has_balance)
    monkeypatch.setattr(
        tts_module.Cost,
        "TTS_PROVIDER_SERVICES",
        {"openai": {"cost_per_character": 0.002, "service_id": 22}},
    )

    result = await tts_module.handle_tts_request(
        websocket=None,
        data={"text": "hello", "author": "bot", "conversationId": 7},
        current_user=type("User", (), {"id": 1})(),
        is_whatsapp=True,
        resolved_voice_override=_voice("openai", "alloy"),
        tts_profile_override=TTSProfile(
            "snapshot-model", "mp3_44100_128", 0.4, 0.8
        ),
    )

    assert result == (None, "insufficient-balance")
    assert requested_amounts == [pytest.approx(0.01)]


@pytest.mark.asyncio
async def test_uncatalogued_preview_requires_admin_path_and_explicit_provider(monkeypatch):
    async def no_catalog_voice(_voice_code):
        return None

    monkeypatch.setattr(tts_module, "resolve_catalog_voice", no_catalog_voice)

    with pytest.raises(tts_module.CanonicalVoiceResolutionError) as ordinary:
        await tts_module.resolve_playback_voice("external-id")
    assert ordinary.value.code == "voice_not_catalogued"

    with pytest.raises(tts_module.CanonicalVoiceResolutionError) as no_provider:
        await tts_module.resolve_playback_voice(
            "external-id", allow_uncatalogued_preview=True
        )
    assert no_provider.value.code == "preview_voice_provider_required"

    preview = await tts_module.resolve_playback_voice(
        "external-id",
        allow_uncatalogued_preview=True,
        preview_provider="elevenlabs",
    )
    assert preview.voice_code == "external-id"
    assert preview.provider == "elevenlabs"


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ('{"detail":{"status":"voice_not_found"}}', True),
        ('{"detail":{"code":"invalid_voice_id"}}', True),
        ('{"detail":{"status":"invalid_model_id"}}', False),
        ('{"detail":{"message":"output_format is invalid"}}', False),
        ("unprocessable entity", False),
    ],
)
def test_elevenlabs_deprecation_requires_explicit_missing_voice_error(body, expected):
    assert tts_module._elevenlabs_error_indicates_missing_voice(body) is expected


@pytest.mark.asyncio
async def test_elevenlabs_generators_request_a_key_for_the_exact_voice(monkeypatch):
    requested = []

    def key_for_voice(voice_id):
        requested.append(voice_id)
        return None

    monkeypatch.setattr(tts_module, "get_elevenlabs_key", key_for_voice)

    with pytest.raises(ValueError, match="No valid Elevenlabs API key"):
        async for _ in tts_module._elevenlabs_http_generator(
            "http-voice", ["hello"]
        ):
            pass
    with pytest.raises(ValueError, match="No valid ElevenLabs API key"):
        async for _ in tts_module.elevenlabs_ws_generator(
            "websocket-voice", ["hello"]
        ):
            pass

    assert requested == ["http-voice", "websocket-voice"]


@pytest.mark.asyncio
async def test_http_generator_uses_preflight_key_then_resumes_voice_aware_balance(
    monkeypatch,
):
    headers_seen = []
    urls_seen = []
    selections = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def read(self):
            return b"audio"

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, url, **kwargs):
            urls_seen.append(url)
            headers_seen.append(kwargs["headers"]["xi-api-key"])
            return Response()

    async def select(voice_id):
        selections.append(voice_id)
        return "balanced-second-key"

    monkeypatch.setattr(tts_module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(tts_module, "_select_elevenlabs_key", select)

    chunks = [
        chunk
        async for chunk in tts_module._elevenlabs_http_generator(
            "exact-voice",
            ["first", "second"],
            elevenlabs_key="preflight-key",
        )
    ]

    assert chunks == [b"audio", b"audio"]
    assert urls_seen == [
        "https://api.elevenlabs.io/v1/text-to-speech/exact-voice/stream"
        "?output_format=opus_48000_128",
        "https://api.elevenlabs.io/v1/text-to-speech/exact-voice/stream"
        "?output_format=opus_48000_128",
    ]
    assert headers_seen == ["preflight-key", "balanced-second-key"]
    assert selections == ["exact-voice"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "should_deprecate"),
    [
        ('{"detail":{"status":"invalid_model_id"}}', False),
        ('{"detail":{"status":"voice_not_found"}}', True),
    ],
)
async def test_http_generator_only_deprecates_explicit_missing_voice(
    monkeypatch, body, should_deprecate
):
    class Response:
        status = 400

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def text(self):
            return body

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def post(self, *_args, **_kwargs):
            return Response()

    deprecated = AsyncMock()
    monkeypatch.setattr(tts_module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(tts_module, "get_elevenlabs_key", lambda: "test-key")
    monkeypatch.setattr(tts_module, "mark_voice_deprecated", deprecated)
    voice = _voice("elevenlabs", "voice-id")

    with pytest.raises(ValueError, match="Elevenlabs API error"):
        async for _ in tts_module._elevenlabs_http_generator(
            voice.voice_code, ["hello"], resolved_voice=voice
        ):
            pass

    if should_deprecate:
        deprecated.assert_awaited_once_with(voice, provider="elevenlabs")
    else:
        deprecated.assert_not_awaited()
