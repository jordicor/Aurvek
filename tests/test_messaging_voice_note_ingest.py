"""Focused tests for WhatsApp/Telegram voice-note ingest preparation."""

from pathlib import Path
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import integrations.telegram.routes as telegram_routes
import integrations.whatsapp.routes as whatsapp_routes
from i18n import LANGUAGES, Translator, get_catalogs, message_parameters
from storage_quota import StorageQuotaExceededError


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("channel", ("telegram", "whatsapp"))
async def test_native_channel_help_uses_recipient_locale(monkeypatch, language, channel):
    module = telegram_routes if channel == "telegram" else whatsapp_routes
    user = SimpleNamespace(id=7, username="untouched-user", is_enabled=True, ui_language=language)
    lookup = "get_user_from_telegram_chat_id" if channel == "telegram" else "get_user_from_phone_number"
    monkeypatch.setattr(module, lookup, AsyncMock(return_value=user))
    if channel == "telegram":
        module._telegram_global_timestamps.clear()
        module._telegram_rate_limits.clear()
    transport = SimpleNamespace(send_message=AsyncMock())
    # A provider's HTTP headers and Telegram language_code are not UI preferences.
    request = SimpleNamespace(headers={"accept-language": "ja"})
    if channel == "telegram":
        await module._process_telegram_message(request, {}, {
            "chat": {"id": 7701}, "from": {"language_code": "ja"}, "text": "!help",
        }, telegram_client=transport)
        delivered = transport.send_message.await_args.args[1]
    else:
        await module._process_whatsapp_message(request, {
            "From": "whatsapp:+15550007701", "To": "whatsapp:+15550007702", "Body": "!help",
        }, twilio_client=transport)
        delivered = transport.send_message.await_args.kwargs["body"]
    assert delivered == Translator(language).render(f"channel_notices.help_{channel}")
    assert all(command in delivered for command in ("!help", "!text", "!voice", "!chats", "!set", "!prompt", "!new"))


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("channel", ("telegram", "whatsapp"))
async def test_application_channel_audio_failure_keeps_recipient_locale(monkeypatch, language, channel):
    module = telegram_routes if channel == "telegram" else whatsapp_routes
    user = SimpleNamespace(id=7, username="synthetic", is_enabled=True, ui_language=language)
    monkeypatch.setattr(module, "get_user_by_id", AsyncMock(return_value=user))
    monkeypatch.setattr(module, "check_messaging", AsyncMock())
    monkeypatch.setattr(module, "application_command", AsyncMock(return_value=None))
    if channel == "telegram":
        module._telegram_global_timestamps.clear()
        module._telegram_rate_limits.clear()
    state = SimpleNamespace(admission=SimpleNamespace(
        scope=SimpleNamespace(user_id=7, conversation_id=11), response_mode="text"))
    transport = SimpleNamespace(send_message=AsyncMock(),
        get_file=AsyncMock(return_value={"file_path": "synthetic.ogg"}),
        download_file=AsyncMock(return_value=b"synthetic"))
    request = SimpleNamespace(headers={"accept-language": "en"})
    if channel == "telegram":
        monkeypatch.setattr(module, "_prepare_telegram_voice", AsyncMock(side_effect=RuntimeError("private provider detail")))
        await module._process_telegram_message(request, {}, {
            "chat": {"id": 7701}, "voice": {"file_id": "synthetic"},
        }, application_state=state, telegram_client=transport)
        delivered = transport.send_message.await_args.args[1]
    else:
        monkeypatch.setattr(module, "validate_twilio_media_url", lambda value: True)
        monkeypatch.setattr(module, "_prepare_whatsapp_audio", AsyncMock(side_effect=RuntimeError("private provider detail")))
        await module._process_whatsapp_message(request, {
            "From": "whatsapp:+15550007701", "To": "whatsapp:+15550007702",
            "MediaUrl0": "https://example.invalid/synthetic", "MediaContentType0": "audio/ogg",
        }, application_state=state, twilio_client=transport)
        delivered = transport.send_message.await_args.kwargs["body"]
    assert delivered == Translator(language).render("channel_notices.audio_failed")
    assert "private provider" not in delivered
    module.get_user_by_id.assert_awaited_once_with(7)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ("telegram",))
async def test_channel_cooldown_resolves_recipient_before_notice(monkeypatch, channel):
    import time
    module = telegram_routes if channel == "telegram" else whatsapp_routes
    lookup = "get_user_from_telegram_chat_id" if channel == "telegram" else "get_user_from_phone_number"
    monkeypatch.setattr(module, lookup, AsyncMock(return_value=SimpleNamespace(ui_language="de")))
    getattr(module, f"_{channel}_global_timestamps").clear()
    getattr(module, f"_{channel}_rate_limit_notices").clear()
    key = "native:7701" if channel == "telegram" else "whatsapp:+15550007701"
    monkeypatch.setitem(getattr(module, f"_{channel}_rate_limits"), key,
                        [time.time()] * getattr(module, f"{channel.upper()}_RATE_LIMIT_PER_USER"))
    transport = SimpleNamespace(send_message=AsyncMock())
    if channel == "telegram":
        await module._process_telegram_message(None, {}, {"chat": {"id": 7701}, "text": "!help"}, telegram_client=transport)
        delivered = transport.send_message.await_args.args[1]
    else:
        await module._process_whatsapp_message(None, {"From": key, "To": "whatsapp:+15550007702", "Body": "!help"}, twilio_client=transport)
        delivered = transport.send_message.await_args.kwargs["body"]
    assert delivered == Translator("de").render("channel_notices.rate_limit" + ("_whatsapp" if channel == "whatsapp" else ""))


@pytest.mark.asyncio
async def test_unknown_and_welcome_overrides_remain_literal(monkeypatch):
    from integrations.whatsapp import service
    override = "Creator text <tag> *literal* {username}"
    monkeypatch.setattr(service, "phone_user_not_found", override)
    assert service.get_phone_user_not_found(Translator("ja")) == override
    monkeypatch.setattr(service, "phone_user_not_found", "")
    assert service.get_phone_user_not_found() == Translator("en").render("channel_notices.whatsapp_unknown")

    telegram_routes._telegram_global_timestamps.clear()
    telegram_routes._telegram_rate_limits.clear()
    monkeypatch.setattr(telegram_routes, "get_user_from_telegram_chat_id", AsyncMock(return_value=None))
    monkeypatch.setattr(telegram_routes, "get_user_from_phone_number", AsyncMock(return_value=SimpleNamespace(
        id=7, username="Nombre", is_enabled=True, ui_language="es")))
    monkeypatch.setattr(telegram_routes, "_log_telegram", AsyncMock())
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(fetchone=AsyncMock(return_value=(override,)))), commit=AsyncMock())
    @asynccontextmanager
    async def connection(**kwargs):
        yield db
    monkeypatch.setattr(telegram_routes, "get_db_connection", connection)
    transport = SimpleNamespace(send_message=AsyncMock())
    await telegram_routes._process_telegram_message(None, {}, {
        "chat": {"id": 7701}, "from": {"id": 7701},
        "contact": {"user_id": 7701, "phone_number": "+15550007701"},
    }, telegram_client=transport)
    assert transport.send_message.await_args.args[1] == override.replace("{username}", "Nombre")


def test_channel_catalog_contracts_and_localized_quota():
    from ai_runtime.messages import (storage_quota_notice_from_response,
        STORAGE_QUOTA_SKIP_HEADER, STORAGE_QUOTA_USAGE_HEADER, STORAGE_QUOTA_LIMIT_HEADER)
    catalogs = get_catalogs()
    english = catalogs["en"]["channel_notices"]
    for language in LANGUAGES:
        assert set(catalogs[language]["channel_notices"]) == set(english)
        for key, pattern in english.items():
            params = {name: (2 if name == "count" else "DATA <&>") for name in message_parameters(pattern)}
            Translator(language).render("channel_notices." + key, **params)
    response = SimpleNamespace(headers={STORAGE_QUOTA_SKIP_HEADER: "1", STORAGE_QUOTA_USAGE_HEADER: "100", STORAGE_QUOTA_LIMIT_HEADER: "50"})
    assert "almacenamiento" in storage_quota_notice_from_response(response, translator=Translator("es"))


@pytest.mark.asyncio
@pytest.mark.parametrize("language", LANGUAGES)
async def test_background_channel_error_delivery_hides_diagnostics(monkeypatch, language):
    from integrations import delivery
    tr = Translator(language)
    transport = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(delivery, "async_telegram", transport)
    context = {"chat_id": 7701, "answer_mode": "text"}
    await delivery.send_platform_error("telegram", context, "insufficient_balance", translator=tr)
    assert transport.send_message.await_args.args[1] == tr.render("chat_errors.insufficient_balance")
    await delivery.send_platform_error("telegram", context, "private provider URL and diagnostic", translator=tr)
    assert transport.send_message.await_args.args[1] == tr.render("chat_errors.request_failed")
    await delivery.send_platform_error("telegram", context, "gransabio_own_keys", translator=tr)
    assert transport.send_message.await_args.args[1] == tr.render("channel_notices.gransabio_own_keys")


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
    download.assert_awaited_once_with(
        "https://media.invalid/voice", max_bytes=16 * 1024 * 1024, total_timeout=60,
    )
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
