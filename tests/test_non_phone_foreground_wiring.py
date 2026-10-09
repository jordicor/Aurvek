from contextlib import asynccontextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import aiosqlite
import jwt
import orjson
import pytest
import pytest_asyncio

import auth
import common
import database
import file_storage
import gransabio_config
import gransabio_service
import save_images
from ai_runtime.context import assembly as context_assembly
from ai_runtime.context import message_provenance
from integrations import delivery as platform_delivery
from integrations.telephony import foreground as foreground_module
from integrations.telephony.repository import TelephonyRepository


ROOT = Path(__file__).resolve().parents[1]


@pytest_asyncio.fixture
async def gransabio_delivery(tmp_path, monkeypatch):
    db_path = tmp_path / "delivery.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript((ROOT / "aurvek_schema.sql").read_text(encoding="utf-8"))
        connection.executescript("""
            PRAGMA foreign_keys=ON;
            INSERT INTO USERS(id,username,is_enabled,ui_language) VALUES(7,'recipient',1,'es');
            INSERT INTO PROMPTS(id,prompt,gransabio_enabled) VALUES(1,'Assistant',1);
            INSERT INTO USER_DETAILS(user_id,current_prompt_id,api_key_mode) VALUES(7,1,'system_only');
            INSERT INTO CONVERSATIONS(id,user_id,role_id,chat_name) VALUES(10,7,1,'Delivery');
            INSERT INTO PHONE_CONVERSATION_FOREGROUND(conversation_id,epoch) VALUES(10,4);
            INSERT INTO MESSAGES(id,conversation_id,user_id,message,type) VALUES
                (1,10,7,'inbound','user'),(2,10,7,'persisted response','bot');
            INSERT INTO SYSTEM_CONFIG(key,value) VALUES('gransabio_enabled','true');
        """)

    @asynccontextmanager
    async def connect(readonly=False):
        async with aiosqlite.connect(db_path) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys=ON")
            yield connection

    for module in (database, auth, context_assembly, message_provenance,
                   gransabio_config, gransabio_service):
        monkeypatch.setattr(module, "get_db_connection", connect)
    monkeypatch.setattr(gransabio_config, "_config_cache", None)
    coordinator = foreground_module.ForegroundCoordinator(TelephonyRepository(connect))
    monkeypatch.setattr(foreground_module, "ForegroundCoordinator", lambda: coordinator)
    lookup = AsyncMock(wraps=auth.get_user_by_id)
    monkeypatch.setattr(auth, "get_user_by_id", lookup)
    monkeypatch.setattr(context_assembly, "apply_rate_limit", AsyncMock(return_value=(True, None)))
    # Prompt assembly and generation have their own tests. Here the generator
    # supplies the already-persisted SSE result at the delivery boundary.
    monkeypatch.setattr(context_assembly, "build_full_prompt_context", AsyncMock(return_value={
        "action": "continue", "full_prompt": "Assistant", "gransabio_config_raw": None,
        "watchdog_config": None, "watchdog_hint_active": False, "watchdog_hint_eval_id": None,
    }))
    case = SimpleNamespace(text="A response for this recipient", stale=False, connect=connect)

    async def generate(**kwargs):
        case.user = kwargs["current_user"]
        yield "data: " + orjson.dumps({"content": case.text, "message_ids": {"user": 1, "bot": 2}}).decode() + "\n\n"
        if case.stale:
            async with connect() as connection:
                await connection.execute("UPDATE PHONE_CONVERSATION_FOREGROUND SET epoch=5 WHERE conversation_id=10")
                await connection.commit()

    monkeypatch.setattr(gransabio_service, "generate_via_gransabio", generate)
    case.transport = SimpleNamespace(send_message=AsyncMock(), send_voice=AsyncMock())
    monkeypatch.setattr(platform_delivery, "async_telegram", case.transport)
    monkeypatch.setattr(platform_delivery, "async_twilio", case.transport)
    audio_path = tmp_path / "tts" / "response.opus"
    audio_path.parent.mkdir()
    audio_path.write_bytes(b"synthetic-audio")
    monkeypatch.setattr(platform_delivery, "cache_directory", audio_path.parent)
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "aurvek.example")
    case.tts = AsyncMock(return_value=(str(audio_path), None))
    monkeypatch.setattr(platform_delivery, "handle_tts_request", case.tts)

    async with aiosqlite.connect(":memory:") as tokens:
        await tokens.execute("CREATE TABLE last_access(user_id INTEGER PRIMARY KEY,last_access TEXT,token TEXT)")
        monkeypatch.setattr(save_images, "USE_REDIS", False)
        monkeypatch.setattr(save_images, "conn_mem", tokens)
        yield case


async def _run_gransabio_delivery(platform: str, *, answer_mode="voice"):
    await gransabio_service.process_gransabio_external(
        conversation_id=10, user_id=7, user_message="inbound", platform=platform,
        platform_context={"conversation_id": 10, "answer_mode": answer_mode, "chat_id": 7701,
                          "from_number": "whatsapp:+15550007701", "to_number": "whatsapp:+15550007702"},
        foreground_epoch=4,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ("telegram", "whatsapp"))
async def test_gransabio_voice_delivers_audio_with_resolved_recipient(gransabio_delivery, platform):
    case = gransabio_delivery
    await _run_gransabio_delivery(platform)
    auth.get_user_by_id.assert_awaited_once_with(7)
    case.tts.assert_awaited_once()
    assert case.tts.await_args.args[2] is case.user
    assert case.user.id == 7
    assert case.tts.await_args.args[1]["text"] == case.text
    if platform == "telegram":
        case.transport.send_voice.assert_awaited_once_with(7701, b"synthetic-audio")
        case.transport.send_message.assert_not_awaited()
    else:
        case.transport.send_message.assert_awaited_once()
        delivered = case.transport.send_message.await_args.kwargs
        parsed = urlparse(delivered["media_url"][0])
        assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "aurvek.example", "/get-audio/response.opus")
        token = parse_qs(parsed.query)["token"][0]
        assert jwt.decode(token, common.SECRET_KEY, algorithms=[common.ALGORITHM])["username"] == case.user.username
        assert (delivered["from_"], delivered["to"]) == ("whatsapp:+15550007702", "whatsapp:+15550007701")


@pytest.mark.asyncio
@pytest.mark.parametrize(("platform", "answer_mode"), (
    ("whatsapp", "text"), ("whatsapp", "voice"), ("telegram", "voice"),
))
async def test_gransabio_long_text_and_voice_fallback_preserve_response(
    gransabio_delivery, platform, answer_mode,
):
    case = gransabio_delivery
    case.text = " ".join(f"Part {index}: contenido completo." for index in range(180))
    if platform == "whatsapp":
        # Preserve literal markup as well as a tail after a long sentence;
        # the TTS splitter stripped tags and could exceed Twilio's limit.
        case.text = "Use this HTML: <button>Send</button>. " + "a " * 450 + "b" * 99 + "." + " c" * 349 + "c"
    case.tts.return_value = (None, "synthetic TTS failure")
    await _run_gransabio_delivery(platform, answer_mode=answer_mode)
    case.transport.send_voice.assert_not_awaited()
    bodies = [
        call.kwargs["body"] if platform == "whatsapp" else call.args[1]
        for call in case.transport.send_message.await_args_list
    ]
    assert len(bodies) > 1
    assert "".join(bodies) == case.text
    assert all(0 < len(body) <= (1600 if platform == "whatsapp" else 3800) for body in bodies)
    if answer_mode == "voice":
        case.tts.assert_awaited_once()
    else:
        case.tts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("platform", "failure"), (
    ("telegram", "rate_limit"), ("whatsapp", "recipient_lookup"),
    ("telegram", "moderation"),
))
async def test_gransabio_voice_service_notices_are_text_without_tts(
    gransabio_delivery, monkeypatch, platform, failure,
):
    case = gransabio_delivery
    if failure == "rate_limit":
        context_assembly.apply_rate_limit.return_value = (False, "private rate diagnostic")
    elif failure == "recipient_lookup":
        auth.get_user_by_id.side_effect = RuntimeError("private lookup diagnostic")
    else:
        monkeypatch.setattr(context_assembly, "run_input_moderation", AsyncMock(return_value=(True, None)))
    await _run_gransabio_delivery(platform)
    auth.get_user_by_id.assert_awaited_once_with(7)
    case.tts.assert_not_awaited()
    case.transport.send_voice.assert_not_awaited()
    case.transport.send_message.assert_awaited_once()
    call = case.transport.send_message.await_args
    body = call.kwargs["body"] if platform == "whatsapp" else call.args[1]
    assert body and "private" not in body
    assert "media_url" not in call.kwargs


@pytest.mark.asyncio
async def test_gransabio_stale_response_never_starts_tts_or_delivery(gransabio_delivery):
    case = gransabio_delivery
    case.stale = True
    await _run_gransabio_delivery("whatsapp")
    assert case.user.id == 7
    case.tts.assert_not_awaited()
    case.transport.send_message.assert_not_awaited()
    case.transport.send_voice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ("telegram", "whatsapp"))
async def test_gransabio_foreground_change_during_tts_suppresses_audio_send(
    gransabio_delivery, platform,
):
    case = gransabio_delivery

    async def synthesize(*_args, **_kwargs):
        async with case.connect() as connection:
            await connection.execute("UPDATE PHONE_CONVERSATION_FOREGROUND SET epoch=5 WHERE conversation_id=10")
            await connection.commit()
        return case.tts.return_value

    case.tts.side_effect = synthesize
    await _run_gransabio_delivery(platform)
    case.tts.assert_awaited_once()
    case.transport.send_message.assert_not_awaited()
    case.transport.send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_gransabio_checks_foreground_before_each_whatsapp_fragment(gransabio_delivery):
    case = gransabio_delivery
    case.text = " ".join(f"Response fragment {index}." for index in range(100))

    async def change_foreground(**_kwargs):
        async with case.connect() as connection:
            await connection.execute("UPDATE PHONE_CONVERSATION_FOREGROUND SET epoch=5 WHERE conversation_id=10")
            await connection.commit()

    case.transport.send_message.side_effect = change_foreground
    await _run_gransabio_delivery("whatsapp", answer_mode="text")
    case.tts.assert_not_awaited()
    case.transport.send_message.assert_awaited_once()
    assert len(case.transport.send_message.await_args.kwargs["body"]) < len(case.text)


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
async def test_gransabio_early_service_notice_resolves_user_once(monkeypatch):
    import auth
    from types import SimpleNamespace
    from ai_runtime.context import assembly
    coordinator = SimpleNamespace(commit_guard_is_current=AsyncMock(return_value=True))
    monkeypatch.setattr(foreground_module, "ForegroundCoordinator", lambda: coordinator)
    user_lookup = AsyncMock(return_value=SimpleNamespace(ui_language="fr"))
    monkeypatch.setattr(auth, "get_user_by_id", user_lookup)
    monkeypatch.setattr(assembly, "check_own_only_gransabio", AsyncMock(return_value="internal own-key diagnostic"))
    transport = AsyncMock()
    monkeypatch.setattr(gransabio_service, "_send_platform_error", transport)
    await gransabio_service._process_gransabio_external_inner(
        conversation_id=10, user_id=7, user_message="literal user text",
        platform="telegram", platform_context={}, foreground_epoch=4,
    )
    user_lookup.assert_awaited_once_with(7)
    assert transport.await_args.args[2] == "gransabio_own_keys"
    assert transport.await_args.kwargs["translator"].language == "fr"


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
    import auth
    from types import SimpleNamespace
    monkeypatch.setattr(auth, "get_user_by_id", AsyncMock(return_value=SimpleNamespace(ui_language="es")))

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
    transport = SimpleNamespace(send_message=AsyncMock())
    monkeypatch.setattr(platform_delivery, "async_twilio", transport)
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
    transport.send_message.assert_not_awaited()
