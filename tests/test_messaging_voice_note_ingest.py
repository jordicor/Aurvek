"""Focused tests for WhatsApp/Telegram voice-note ingest preparation."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import integrations.telegram.routes as telegram_routes
import integrations.whatsapp.routes as whatsapp_routes
from storage_quota import StorageQuotaExceededError


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_external_message_ids_do_not_collide_across_chats() -> None:
    assert telegram_routes._telegram_external_message_id(
        update_id=9001,
        chat_id=10,
        message_id=1,
    ) == "update:9001"
    assert telegram_routes._telegram_external_message_id(
        update_id=None,
        chat_id=10,
        message_id=1,
    ) == "chat:10:message:1"
    assert telegram_routes._telegram_external_message_id(
        update_id=None,
        chat_id=20,
        message_id=1,
    ) == "chat:20:message:1"


@pytest.mark.parametrize(
    ("mime_type", "expected"),
    (
        ("audio/ogg; codecs=opus", "voice_note"),
        ("audio/opus", "voice_note"),
        ("audio/amr", "voice_note"),
        ("audio/mpeg", "audio"),
        ("audio/mp4", "audio"),
    ),
)
def test_whatsapp_voice_note_classification_is_mime_scoped(
    mime_type: str,
    expected: str,
) -> None:
    assert whatsapp_routes._whatsapp_audio_content_kind(mime_type) == expected


@pytest.mark.asyncio
async def test_whatsapp_retention_off_downloads_once_without_creating_file(
    monkeypatch,
) -> None:
    audio = b"one-download"
    download = AsyncMock(return_value=audio)
    retention = AsyncMock(return_value=False)
    create = AsyncMock(side_effect=AssertionError("retention is disabled"))
    transcribe = AsyncMock(
        return_value=SimpleNamespace(
            text="hello",
            provider="elevenlabs",
            model="scribe_v2",
            duration_seconds=12.5,
        )
    )
    monkeypatch.setattr(whatsapp_routes, "download_external_audio", download)
    monkeypatch.setattr(
        whatsapp_routes,
        "get_voice_note_retention_enabled",
        retention,
    )
    monkeypatch.setattr(whatsapp_routes, "create_pending_audio_attachment", create)
    monkeypatch.setattr(
        whatsapp_routes,
        "transcribe_external_audio_detailed",
        transcribe,
    )

    text, metadata = await whatsapp_routes._prepare_whatsapp_audio(
        user_id=7,
        conversation_id=11,
        media_url="https://media.invalid/voice",
        media_type="audio/ogg; codecs=opus",
        user_agent="test-agent",
    )

    assert text == "hello"
    assert metadata == {
        "transcript": "hello",
        "stt_provider": "elevenlabs",
        "stt_model": "scribe_v2",
        "duration_seconds": 12.5,
        "retention_status": "disabled",
        "audio_attachment_ref": None,
    }
    download.assert_awaited_once_with("https://media.invalid/voice")
    retention.assert_awaited_once_with("whatsapp")
    create.assert_not_awaited()
    assert transcribe.await_args.kwargs["audio_content"] is audio


@pytest.mark.asyncio
async def test_telegram_retention_on_returns_stored_attachment_reference(
    monkeypatch,
) -> None:
    audio = b"retained-telegram-audio"
    monkeypatch.setattr(
        telegram_routes,
        "get_voice_note_retention_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        telegram_routes,
        "create_pending_audio_attachment",
        AsyncMock(return_value=SimpleNamespace(public_id="att_stored")),
    )
    monkeypatch.setattr(
        telegram_routes,
        "transcribe_external_audio_detailed",
        AsyncMock(
            return_value=SimpleNamespace(
                text="stored transcript",
                provider="elevenlabs",
                model="scribe_v2",
                duration_seconds=45.0,
            )
        ),
    )

    _, metadata = await telegram_routes._prepare_telegram_voice(
        user_id=8,
        conversation_id=12,
        audio_content=audio,
        mime_type="audio/ogg",
    )

    assert metadata["retention_status"] == "stored"
    assert metadata["audio_attachment_ref"] == "att_stored"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("storage_error", "expected_status"),
    (
        (StorageQuotaExceededError(100, 50, "quota"), "quota_skipped"),
        (RuntimeError("storage unavailable"), "failed"),
    ),
)
async def test_telegram_storage_failure_does_not_lose_transcription(
    monkeypatch,
    storage_error: Exception,
    expected_status: str,
) -> None:
    audio = b"telegram-audio"
    monkeypatch.setattr(
        telegram_routes,
        "get_voice_note_retention_enabled",
        AsyncMock(return_value=True),
    )
    create = AsyncMock(side_effect=storage_error)
    monkeypatch.setattr(telegram_routes, "create_pending_audio_attachment", create)
    monkeypatch.setattr(
        telegram_routes,
        "transcribe_external_audio_detailed",
        AsyncMock(
            return_value=SimpleNamespace(
                text="telegram transcript",
                provider="deepgram",
                model="nova-2",
                duration_seconds=90.0,
            )
        ),
    )

    text, metadata = await telegram_routes._prepare_telegram_voice(
        user_id=8,
        conversation_id=12,
        audio_content=audio,
        mime_type="audio/ogg",
    )

    assert text == "telegram transcript"
    assert metadata["retention_status"] == expected_status
    assert metadata["audio_attachment_ref"] is None
    assert create.await_args.kwargs["data"] is audio


@pytest.mark.asyncio
async def test_whatsapp_transcription_failure_discards_pending_audio(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        whatsapp_routes,
        "download_external_audio",
        AsyncMock(return_value=b"audio"),
    )
    monkeypatch.setattr(
        whatsapp_routes,
        "get_voice_note_retention_enabled",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        whatsapp_routes,
        "create_pending_audio_attachment",
        AsyncMock(return_value=SimpleNamespace(public_id="att_pending")),
    )
    monkeypatch.setattr(
        whatsapp_routes,
        "transcribe_external_audio_detailed",
        AsyncMock(side_effect=RuntimeError("STT failed")),
    )
    discard = AsyncMock()
    monkeypatch.setattr(whatsapp_routes, "discard_pending_attachments", discard)

    with pytest.raises(RuntimeError, match="STT failed"):
        await whatsapp_routes._prepare_whatsapp_audio(
            user_id=9,
            conversation_id=13,
            media_url="https://media.invalid/voice",
            media_type="audio/ogg",
            user_agent=None,
        )

    discard.assert_awaited_once_with(
        ["att_pending"],
        "whatsapp_transcription_failed",
    )


def test_routes_and_gransabio_transport_only_serializable_provenance() -> None:
    whatsapp = (ROOT / "integrations/whatsapp/routes.py").read_text(encoding="utf-8")
    telegram = (ROOT / "integrations/telegram/routes.py").read_text(encoding="utf-8")
    gransabio = (ROOT / "gransabio_service.py").read_text(encoding="utf-8")

    for source in (whatsapp, telegram):
        assert '"message_provenance": message_provenance' in source
        assert "channel_context=channel_context" in source

    restore = gransabio.index("external_channel_context = restore_non_phone_generation_context")
    provenance = gransabio.index('platform_context.get("message_provenance")', restore)
    attach = gransabio.index("attach_message_channel_provenance(", provenance)
    assert restore < provenance < attach
