from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from chat.routes import voice_io
from i18n import Translator


class AudioUpload:
    async def read(self):
        return b"audio"


def _prepare_audio(monkeypatch):
    monkeypatch.setattr(voice_io, "get_browser", lambda user_agent: "chrome")
    monkeypatch.setattr(
        voice_io,
        "_decode_audio_duration",
        lambda *args, **kwargs: 60,
    )
    monkeypatch.setattr(
        voice_io,
        "_load_primary_stt_language",
        AsyncMock(return_value="es"),
    )


async def _transcribe(language="es", audio=None):
    return await voice_io.transcribe(
        SimpleNamespace(headers={"user-agent": "test"}, state=SimpleNamespace()),
        audio=AudioUpload() if audio is None else audio,
        user_id=1,
        ui_language=language,
    )


@pytest.mark.asyncio
async def test_provider_http_error_is_private_localized_and_keeps_status(monkeypatch):
    _prepare_audio(monkeypatch)
    monkeypatch.setattr(voice_io, "stt_engine", "elevenlabs")
    monkeypatch.setattr(voice_io, "reserve_stt_attempt", AsyncMock(return_value="hold"))
    monkeypatch.setattr(voice_io, "finalize_failed_stt_attempt", AsyncMock())
    monkeypatch.setattr(
        voice_io,
        "transcribe_with_elevenlabs",
        AsyncMock(side_effect=HTTPException(status_code=429, detail="provider account secret")),
    )

    with pytest.raises(HTTPException) as error:
        await _transcribe()

    assert error.value.status_code == 429
    assert error.value.detail == Translator("es").t("chat_errors.provider_unavailable")
    assert "secret" not in error.value.detail
    voice_io.finalize_failed_stt_attempt.assert_awaited_once()


@pytest.mark.asyncio
async def test_insufficient_balance_is_localized_and_keeps_402(monkeypatch):
    _prepare_audio(monkeypatch)
    monkeypatch.setattr(
        voice_io,
        "reserve_stt_attempt",
        AsyncMock(side_effect=HTTPException(status_code=402, detail="private billing detail")),
    )

    with pytest.raises(HTTPException) as error:
        await _transcribe(language="ja")

    assert error.value.status_code == 402
    assert error.value.detail == Translator("ja").t("chat_errors.insufficient_balance")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "audio", "key"),
    [
        ({"user-agent": "test"}, None, "audio_required"),
        ({"user-agent": "unknown"}, AudioUpload(), "browser_unsupported"),
    ],
)
async def test_native_validation_errors_keep_their_specific_message(
    monkeypatch, headers, audio, key
):
    if key == "browser_unsupported":
        monkeypatch.setattr(voice_io, "get_browser", lambda user_agent: "other")

    with pytest.raises(HTTPException) as error:
        await voice_io.transcribe(
            SimpleNamespace(headers=headers),
            audio=audio,
            user_id=1,
            ui_language="fr",
        )

    assert error.value.status_code == 400
    assert error.value.detail == Translator("fr").t("chat_errors." + key)
