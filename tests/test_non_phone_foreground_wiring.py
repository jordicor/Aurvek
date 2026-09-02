from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import file_storage
import gransabio_service
from ai_runtime.context import assembly as context_assembly
from integrations.telephony import foreground as foreground_module


ROOT = Path(__file__).resolve().parents[1]


def _function_body(path: str, marker: str) -> str:
    source = (ROOT / path).read_text(encoding="utf-8")
    return source[source.index(marker):]


def test_web_captures_foreground_before_multi_ai_or_normal_generation() -> None:
    body = _function_body(
        "chat/routes/messages.py",
        "async def save_message(",
    )
    capture = body.index("foreground_turn = await capture_non_phone_channel_turn(")
    assert capture < body.index("if multi_ai_models and not foreground_turn.decision.phone_active")
    assert "channel_context=foreground_turn.context" in body


def test_external_channels_capture_before_gransabio_and_pass_runtime_context() -> None:
    for path, function_marker, channel in (
        ("integrations/whatsapp/routes.py", "async def whatsapp_webhook(", "whatsapp"),
        ("integrations/telegram/routes.py", "async def telegram_webhook(", "telegram"),
    ):
        body = _function_body(path, function_marker)
        capture = body.index("foreground_turn = await capture_non_phone_channel_turn(")
        gransabio = body.index("# --- GranSabio check: if enabled")
        assert capture < gransabio
        assert f'channel="{channel}"' in body[capture:gransabio]
        assert "channel_context = attach_message_channel_provenance(" in body
        assert "foreground_turn.context," in body
        assert "channel_context=channel_context" in body
        assert "foreground_epoch=foreground_turn.decision.commit_guard.epoch" in body
        assert "and not foreground_turn.decision.phone_active" in body
        assert body.count("if not await foreground_turn.is_current():") >= 2
        assert body.index("except StaleChannelTurnError:") > gransabio


def test_device_captures_before_runtime_and_handles_queued_terminal() -> None:
    body = _function_body(
        "integrations/devices/service.py",
        "async def handle_device_text_message(",
    )
    capture = body.index("foreground_turn = await capture_non_phone_channel_turn(")
    runtime = body.index("process_save_message(", capture)
    assert capture < runtime
    assert "channel_context=foreground_turn.context" in body[runtime:]
    assert 'runtime_terminal == "queued_for_active_phone"' in body


def test_background_gransabio_transports_and_binds_serialized_foreground_epoch() -> None:
    tasks = (ROOT / "tasks.py").read_text(encoding="utf-8")
    service = (ROOT / "gransabio_service.py").read_text(encoding="utf-8")
    assert "foreground_epoch: int | None = None" in tasks
    assert "foreground_epoch=foreground_epoch" in tasks
    assert "restore_non_phone_generation_context(" in service
    assert "channel_context=external_channel_context" in service
    assert "except StaleChannelTurnError:" in service


def test_long_voice_note_actor_overrides_global_age_and_time_limits() -> None:
    tasks = (ROOT / "tasks.py").read_text(encoding="utf-8")
    actor = tasks[tasks.index("def retranscribe_voice_note_task"):]
    decorator = tasks[:tasks.index("def retranscribe_voice_note_task")]
    assert "max_age=None" in decorator[-250:]
    assert "time_limit=28_800_000" in decorator[-250:]
    assert "run_retranscription_job" in actor


@pytest.mark.asyncio
async def test_gransabio_voice_note_cleanup_is_pending_only_and_owner_scoped(
    monkeypatch,
) -> None:
    discard = AsyncMock(return_value=1)
    monkeypatch.setattr(
        file_storage,
        "discard_pending_attachments_for_user",
        discard,
    )

    await gransabio_service._discard_gransabio_pending_voice_note(
        {
            "message_provenance": {
                "user_id": 7,
                "conversation_id": 19,
                "voice_note": {"audio_attachment_ref": "att_pending_voice"},
            }
        },
        platform="telegram",
    )

    discard.assert_awaited_once_with(
        ["att_pending_voice"],
        user_id=7,
        conversation_id=19,
        reason="telegram_gransabio_finished_without_audio_commit",
    )


def test_browser_treats_phone_queue_as_successful_terminal_without_bot_bubble() -> None:
    frontend = (ROOT / "data/static/js/chat/chat.js").read_text(encoding="utf-8")
    assert "parsedData.terminal === 'queued_for_active_phone'" in frontend
    assert "queuedForActivePhone = true" in frontend
    assert "botMessageElement.remove()" in frontend


def test_browser_removes_provisional_bot_for_unrecovered_stale_turn() -> None:
    frontend = (ROOT / "data/static/js/chat/chat.js").read_text(encoding="utf-8")
    assert "parsedData.terminal === 'stale_channel_turn'" in frontend
    assert "staleChannelTurn = true" in frontend
    assert "streamContentReceived = false" in frontend


@pytest.mark.asyncio
async def test_legacy_gransabio_job_without_epoch_fails_closed(monkeypatch) -> None:
    delivery = AsyncMock()
    error_delivery = AsyncMock()
    monkeypatch.setattr(gransabio_service, "_deliver_to_platform", delivery)
    monkeypatch.setattr(gransabio_service, "_send_platform_error", error_delivery)

    await gransabio_service.process_gransabio_external(
        conversation_id=10,
        user_id=1,
        user_message="legacy inbound",
        platform="whatsapp",
        platform_context={},
        foreground_epoch=None,
    )

    delivery.assert_not_awaited()
    error_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_gransabio_checks_epoch_before_pipeline_and_recovers_stale_inbound(
    monkeypatch,
) -> None:
    class StaleCoordinator:
        async def commit_guard_is_current(self, _guard):
            return False

    recovery = AsyncMock(return_value=True)
    delivery = AsyncMock()
    error_delivery = AsyncMock()
    monkeypatch.setattr(
        foreground_module,
        "ForegroundCoordinator",
        lambda: StaleCoordinator(),
    )
    monkeypatch.setattr(
        gransabio_service,
        "_recover_external_stale_user_turn",
        recovery,
    )
    monkeypatch.setattr(gransabio_service, "_deliver_to_platform", delivery)
    monkeypatch.setattr(gransabio_service, "_send_platform_error", error_delivery)

    await gransabio_service.process_gransabio_external(
        conversation_id=10,
        user_id=1,
        user_message="raced inbound",
        platform="telegram",
        platform_context={},
        foreground_epoch=4,
    )

    recovery.assert_awaited_once()
    delivery.assert_not_awaited()
    error_delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_gransabio_moderation_race_recovers_only_blocked_marker(
    monkeypatch,
) -> None:
    class RacingCoordinator:
        def __init__(self):
            self.checks = 0

        async def commit_guard_is_current(self, _guard):
            self.checks += 1
            return self.checks == 1

    class Cursor:
        async def fetchone(self):
            return (7, 1, None, 1, "prompt", None, None, None)

    class Connection:
        async def execute(self, *_args, **_kwargs):
            return Cursor()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def db_connection(*_args, **_kwargs):
        yield Connection()

    async def no_own_only_error(*_args, **_kwargs):
        return None

    async def rate_allowed(*_args, **_kwargs):
        return True, None

    async def blocked(*_args, **_kwargs):
        return True, []

    recovery = AsyncMock(return_value=True)
    delivery = AsyncMock()
    monkeypatch.setattr(
        foreground_module,
        "ForegroundCoordinator",
        lambda: RacingCoordinator(),
    )
    monkeypatch.setattr(gransabio_service, "get_db_connection", db_connection)
    monkeypatch.setattr(
        context_assembly,
        "check_own_only_gransabio",
        no_own_only_error,
    )
    monkeypatch.setattr(context_assembly, "apply_rate_limit", rate_allowed)
    monkeypatch.setattr(context_assembly, "run_input_moderation", blocked)
    monkeypatch.setattr(
        gransabio_service,
        "_recover_external_stale_user_turn",
        recovery,
    )
    monkeypatch.setattr(gransabio_service, "_deliver_to_platform", delivery)

    await gransabio_service.process_gransabio_external(
        conversation_id=10,
        user_id=1,
        user_message="original text that moderation blocked",
        platform="whatsapp",
        platform_context={},
        foreground_epoch=8,
    )

    recovery.assert_awaited_once()
    assert recovery.await_args.kwargs["user_message"] == "[Blocked Message]"
    delivery.assert_not_awaited()
