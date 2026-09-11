from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import integrations.media as media


def _stub_audio_decode(monkeypatch) -> None:
    monkeypatch.setattr(media, "_probe_audio_duration_seconds", lambda _audio: 1.0)
    monkeypatch.setattr(
        media,
        "_load_primary_stt_language",
        AsyncMock(return_value=media.DEFAULT_STT_LANGUAGE),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preferred_languages", "expected"),
    [
        ((), None),
        (("pt", "es"), "pt"),
    ],
)
async def test_primary_stt_language_uses_profile_then_provider_autodetection(
    monkeypatch,
    preferred_languages,
    expected,
) -> None:
    connection = object()

    @asynccontextmanager
    async def get_connection(*, readonly=False):
        assert readonly is True
        yield connection

    load_languages = AsyncMock(return_value=preferred_languages)
    monkeypatch.setattr(media, "get_db_connection", get_connection)
    monkeypatch.setattr(media, "load_user_preferred_languages", load_languages)

    assert await media._load_primary_stt_language(31) == expected
    load_languages.assert_awaited_once_with(connection, 31)


@pytest.mark.asyncio
async def test_deepgram_provider_receives_the_resolved_language(monkeypatch) -> None:
    captured = {}

    async def transcribe_file(payload, options, *, timeout):
        captured.update(payload=payload, options=options, timeout=timeout)
        return SimpleNamespace(
            to_dict=lambda: {
                "results": {
                    "channels": [
                        {"alternatives": [{"transcript": "bonjour"}]}
                    ]
                }
            }
        )

    endpoint = SimpleNamespace(transcribe_file=transcribe_file)
    monkeypatch.setattr(
        media,
        "deepgram",
        SimpleNamespace(
            listen=SimpleNamespace(
                asyncprerecorded=SimpleNamespace(v=lambda _version: endpoint)
            )
        ),
    )

    transcript = await media.transcribe_with_deepgram(
        audio_content=b"audio",
        language_code="fr",
    )

    assert transcript == "bonjour"
    assert captured["options"]["language"] == "fr"

    await media.transcribe_with_deepgram(
        audio_content=b"audio",
        language_code=None,
    )
    assert captured["options"]["language"] == media.DEFAULT_STT_LANGUAGE


@pytest.mark.asyncio
async def test_scribe_provider_receives_the_resolved_language(monkeypatch) -> None:
    captured = {}

    class FormData:
        def __init__(self):
            self.fields = {}

        def add_field(self, name, value, **_kwargs):
            self.fields[name] = value

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self):
            return {"text": "hola"}

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def post(self, url, *, headers, data):
            captured.update(url=url, headers=headers, data=data)
            return Response()

    monkeypatch.setattr(media, "get_elevenlabs_key", lambda: "test-key")
    monkeypatch.setattr(media.aiohttp, "FormData", FormData)
    monkeypatch.setattr(media.aiohttp, "ClientSession", Session)

    transcript = await media.transcribe_with_elevenlabs(
        audio_content=b"audio",
        language_code="es",
    )

    assert transcript == "hola"
    assert captured["data"].fields["language_code"] == "es"

    captured.clear()
    transcript = await media.transcribe_with_elevenlabs(
        audio_content=b"audio",
        language_code=None,
    )
    assert transcript == "hola"
    assert "language_code" not in captured["data"].fields


@pytest.mark.asyncio
async def test_external_audio_defaults_to_elevenlabs_when_engine_is_unset(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    reservations = []

    async def reserve_stt_attempt(**values):
        reservations.append(values)
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    async def transcribe_with_elevenlabs(**_kwargs):
        return "scribe transcript"

    async def must_not_use_deepgram(**_kwargs):
        raise AssertionError("Deepgram must require an explicit selection")

    monkeypatch.setattr(media, "stt_engine", None)
    monkeypatch.setattr(media, "stt_fallback_enabled", True)
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(
        media,
        "transcribe_with_elevenlabs",
        transcribe_with_elevenlabs,
    )
    monkeypatch.setattr(
        media,
        "transcribe_with_deepgram",
        must_not_use_deepgram,
    )

    result = await media.transcribe_external_audio(
        user_id=31,
        audio_content=b"synthetic audio",
    )

    assert result == "scribe transcript"
    assert [item["engine"] for item in reservations] == ["elevenlabs"]


@pytest.mark.asyncio
async def test_detailed_external_audio_reports_actual_provider_model_and_duration(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    provider_calls = []

    async def reserve_stt_attempt(**_values):
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    monkeypatch.setattr(media, "stt_engine", "elevenlabs")
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)

    async def deepgram(**kwargs):
        provider_calls.append(kwargs)
        return "new transcript"

    monkeypatch.setattr(
        media,
        "_load_primary_stt_language",
        AsyncMock(return_value="fr"),
    )
    monkeypatch.setattr(media, "transcribe_with_deepgram", deepgram)

    result = await media.transcribe_external_audio_detailed(
        user_id=31,
        audio_content=b"synthetic audio",
        preferred_engine="deepgram",
    )

    assert result.text == "new transcript"
    assert result.provider == "deepgram"
    assert result.model == "nova-2"
    assert result.duration_seconds == 1.0
    assert provider_calls[0]["language_code"] == "fr"


@pytest.mark.asyncio
async def test_external_audio_leaves_scribe_on_auto_without_preferences(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    provider_calls = []

    async def reserve_stt_attempt(**_values):
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    async def elevenlabs(**kwargs):
        provider_calls.append(kwargs)
        return "transcript"

    monkeypatch.setattr(media, "stt_engine", "elevenlabs")
    monkeypatch.setattr(
        media,
        "_load_primary_stt_language",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(media, "transcribe_with_elevenlabs", elevenlabs)

    result = await media.transcribe_external_audio_detailed(
        user_id=31,
        audio_content=b"synthetic audio",
    )

    assert result.text == "transcript"
    assert provider_calls[0]["language_code"] is None


@pytest.mark.asyncio
async def test_known_retained_duration_avoids_redecoding_long_audio(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        media,
        "_probe_audio_duration_seconds",
        lambda _audio: (_ for _ in ()).throw(AssertionError("must not probe")),
    )

    async def reserve_stt_attempt(**values):
        assert values["duration_min"] == pytest.approx(90.0)
        return "reservation"

    async def settle_stt_attempt(*_args, **_kwargs):
        return None

    async def deepgram(**_kwargs):
        return "long transcript"

    monkeypatch.setattr(media, "stt_engine", "deepgram")
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(media, "settle_stt_attempt", settle_stt_attempt)
    monkeypatch.setattr(
        media,
        "_load_primary_stt_language",
        AsyncMock(return_value=media.DEFAULT_STT_LANGUAGE),
    )
    monkeypatch.setattr(media, "transcribe_with_deepgram", deepgram)

    result = await media.transcribe_external_audio_detailed(
        user_id=31,
        audio_content=b"retained compressed audio",
        duration_seconds=5_400,
    )

    assert result.duration_seconds == 5_400


@pytest.mark.asyncio
async def test_external_elevenlabs_failure_never_falls_back_to_deepgram(
    monkeypatch,
) -> None:
    _stub_audio_decode(monkeypatch)
    reservations = []
    failed = []
    deepgram_called = False

    async def reserve_stt_attempt(**values):
        reservations.append(values)
        return "reservation"

    async def finalize_failed_stt_attempt(*args, **kwargs):
        failed.append((args, kwargs))

    async def fail_elevenlabs(**_kwargs):
        raise RuntimeError("synthetic Scribe failure")

    async def track_deepgram(**_kwargs):
        nonlocal deepgram_called
        deepgram_called = True
        return "must not be returned"

    monkeypatch.setattr(media, "stt_engine", "elevenlabs")
    monkeypatch.setattr(media, "stt_fallback_enabled", True)
    monkeypatch.setattr(media, "reserve_stt_attempt", reserve_stt_attempt)
    monkeypatch.setattr(
        media,
        "finalize_failed_stt_attempt",
        finalize_failed_stt_attempt,
    )
    monkeypatch.setattr(media, "transcribe_with_elevenlabs", fail_elevenlabs)
    monkeypatch.setattr(media, "transcribe_with_deepgram", track_deepgram)

    with pytest.raises(RuntimeError, match="synthetic Scribe failure"):
        await media.transcribe_external_audio(
            user_id=31,
            audio_content=b"synthetic audio",
        )

    assert deepgram_called is False
    assert [item["engine"] for item in reservations] == ["elevenlabs"]
    assert len(failed) == 1
