from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest

from ai_runtime.channel_turns import (
    ChannelCommit,
    ChannelContext,
    ChannelPersistenceError,
    ChannelTurnRegistry,
    StaleChannelTurnError,
    TurnKey,
    bind_channel_turn,
)
from ai_runtime import messages as runtime_messages
from ai_runtime.persistence import messages as persistence
import common


def _save_args(content: str = "The complete generated answer") -> tuple:
    return (content, 12, 7, 19, 41, 5, "test-model")


def test_exact_accumulated_api_cost_combines_with_current_provider_usage():
    combined = persistence._combined_reservation_api_cost(
        accumulated_api_cost=0.2,
        current_input_tokens=100,
        current_output_tokens=50,
        input_cost_per_million=10.0,
        output_cost_per_million=20.0,
        current_byok=False,
        current_override_api_cost=None,
    )

    assert combined == pytest.approx(0.202)


def test_exact_accumulated_api_cost_keeps_byok_current_usage_free():
    combined = persistence._combined_reservation_api_cost(
        accumulated_api_cost=0.2,
        current_input_tokens=100,
        current_output_tokens=50,
        input_cost_per_million=10.0,
        output_cost_per_million=20.0,
        current_byok=True,
        current_override_api_cost=0.7,
    )

    assert combined == pytest.approx(0.2)


def test_legacy_accumulated_tokens_keep_current_override_fallback():
    assert persistence._combined_reservation_api_cost(
        accumulated_api_cost=None,
        current_input_tokens=100,
        current_output_tokens=50,
        input_cost_per_million=10.0,
        output_cost_per_million=20.0,
        current_byok=False,
        current_override_api_cost=0.7,
    ) == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_failed_ai_settlement_preserves_hold_for_reconciliation(monkeypatch):
    settlement = AsyncMock(
        side_effect=runtime_messages.BillingReservationError(
            "exact Realtime usage could not be captured"
        )
    )
    refund = AsyncMock()
    monkeypatch.setattr(
        runtime_messages,
        "settle_accumulated_ai_reservation_usage",
        settlement,
    )
    monkeypatch.setattr(runtime_messages, "refund_fixed_usage", refund)

    await runtime_messages._settle_or_refund_ai_reservation(
        reservation_id="ai-realtime-active",
        user_id=41,
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        prompt_id=5,
        byok=False,
    )

    settlement.assert_awaited_once()
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_started_ai_usage_is_not_refunded(monkeypatch):
    settlement = AsyncMock(return_value=False)
    provider_started = AsyncMock(return_value=True)
    refund = AsyncMock()
    monkeypatch.setattr(
        runtime_messages,
        "settle_accumulated_ai_reservation_usage",
        settlement,
    )
    monkeypatch.setattr(
        runtime_messages,
        "ai_reservation_provider_started",
        provider_started,
    )
    monkeypatch.setattr(runtime_messages, "refund_fixed_usage", refund)

    await runtime_messages._settle_or_refund_ai_reservation(
        reservation_id="ai-realtime-uncertain",
        user_id=41,
        input_cost_per_million=0.0,
        output_cost_per_million=0.0,
        prompt_id=5,
        byok=False,
    )

    provider_started.assert_awaited_once_with(
        reservation_id="ai-realtime-uncertain",
        user_id=41,
    )
    refund.assert_not_awaited()


def test_request_free_runtime_url_uses_configured_canonical_domain(monkeypatch):
    monkeypatch.setattr(common, "PRIMARY_APP_DOMAIN", "voice.example.test")
    assert common.get_runtime_request_url(None) == "https://voice.example.test/"
    request = SimpleNamespace(url="https://incoming.example.test/current?x=1")
    assert common.get_runtime_request_url(request) == str(request.url)


def test_request_free_media_tools_use_neutral_url_helper():
    root = Path(__file__).resolve().parents[1]
    for relative in (
        "tools/generate_images.py",
        "tools/generate_videos.py",
        "tools/qr_code.py",
    ):
        source = (root / relative).read_text(encoding="utf-8")
        assert "request_url = get_runtime_request_url(request)" in source
        assert "request_url = str(request.url)" not in source


@pytest.mark.asyncio
async def test_immediate_persistence_is_a_regression_safe_passthrough(monkeypatch):
    immediate = AsyncMock(return_value=(101, 102))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)

    result = await persistence.save_content_to_db(
        *_save_args(), user_message="hello"
    )

    assert result == (101, 102)
    immediate.assert_awaited_once()
    assert immediate.await_args.args[:7] == _save_args()


@pytest.mark.asyncio
async def test_deferred_commit_publishes_draft_and_saves_exact_audible_prefix(monkeypatch):
    immediate = AsyncMock(return_value=(201, 202))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    commits = []
    context = ChannelContext(
        channel="phone",
        persistence="deferred",
        turn_key=TurnKey("call-a", "turn-1"),
        on_commit=commits.append,
        provenance={"speaker": "contact-7"},
    )
    handle = await ChannelTurnRegistry().register(context)

    with bind_channel_turn(context, handle):
        save_task = asyncio.create_task(
            persistence.save_content_to_db(*_save_args(), user_message="hello")
        )
        draft = await handle.wait_for_draft()
        assert draft.content == "The complete generated answer"
        handle.confirm_audible_prefix("The complete", played_ms=640)
        assert await save_task == (201, 202)

    immediate.assert_awaited_once()
    assert immediate.await_args.args[0] == "The complete"
    assert immediate.await_args.kwargs["save_assistant_message"] is True
    assert len(commits) == 1
    assert commits[0].confirmed_text == "The complete"
    assert commits[0].played_ms == 640
    assert commits[0].context.provenance == {"speaker": "contact-7"}


@pytest.mark.asyncio
async def test_zero_ms_confirmation_saves_user_only_without_empty_bot(monkeypatch):
    immediate = AsyncMock(return_value=(301, None))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone",
        persistence="deferred",
        turn_key=TurnKey("call-a", "turn-zero"),
    )
    handle = await ChannelTurnRegistry().register(context)

    with bind_channel_turn(context, handle):
        save_task = asyncio.create_task(
            persistence.save_content_to_db(*_save_args(), user_message="interrupted")
        )
        await handle.wait_for_draft()
        handle.confirm_audible_prefix("", played_ms=0)
        assert await save_task == (301, None)

    assert immediate.await_args.args[0] is None
    assert immediate.await_args.kwargs["save_assistant_message"] is False
    assert immediate.await_args.kwargs["user_message"] == "interrupted"


@pytest.mark.asyncio
async def test_confirmation_must_be_an_exact_prefix_and_match_duration(monkeypatch):
    immediate = AsyncMock(return_value=(1, 2))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("call-a", "turn-prefix"),
    )
    handle = await ChannelTurnRegistry().register(context)

    with bind_channel_turn(context, handle):
        save_task = asyncio.create_task(persistence.save_content_to_db(*_save_args()))
        await handle.wait_for_draft()
        with pytest.raises(ValueError, match="exact prefix"):
            handle.confirm_audible_prefix("complete generated", played_ms=20)
        with pytest.raises(ValueError, match="0 ms"):
            handle.confirm_audible_prefix("The", played_ms=0)
        with pytest.raises(ValueError, match="non-empty"):
            handle.confirm_audible_prefix("", played_ms=20)
        handle.confirm_audible_prefix("The", played_ms=20)
        await save_task


@pytest.mark.asyncio
async def test_stale_commit_guard_runs_immediately_before_and_prevents_write(monkeypatch):
    async def guarded_immediate(*args, **kwargs):
        from ai_runtime.channel_turns import assert_commit_guard_in_transaction

        await assert_commit_guard_in_transaction(kwargs["channel_context"], object())
        return (1, 2)

    immediate = AsyncMock(side_effect=guarded_immediate)
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("call-a", "turn-stale"),
        commit_guard=AsyncMock(return_value=False),
    )
    handle = await ChannelTurnRegistry().register(context)

    with bind_channel_turn(context, handle):
        save_task = asyncio.create_task(persistence.save_content_to_db(*_save_args()))
        await handle.wait_for_draft()
        handle.confirm_audible_prefix("The", played_ms=20)
        with pytest.raises(StaleChannelTurnError):
            await save_task

    # The central transaction function is entered once; its first operation is
    # the guard, so the actual implementation performs no SQL write.
    immediate.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_deferred_save_is_idempotent_for_billing_tools_and_side_effects(monkeypatch):
    transactional_hook = AsyncMock()

    async def immediate_side_effect(*args, **kwargs):
        await kwargs["channel_context"].on_commit_in_transaction(
            ChannelCommit(
                context=kwargs["channel_context"],
                user_message_id=401,
                assistant_message_id=402,
                confirmed_text=kwargs["channel_confirmed_text"],
                played_ms=kwargs["channel_played_ms"],
            ),
            object(),
        )
        return (401, 402)

    immediate = AsyncMock(side_effect=immediate_side_effect)
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("call-a", "turn-idempotent"),
        on_commit_in_transaction=transactional_hook,
    )
    handle = await ChannelTurnRegistry().register(context)

    with bind_channel_turn(context, handle):
        first = asyncio.create_task(persistence.save_content_to_db(*_save_args()))
        second = asyncio.create_task(persistence.save_content_to_db(*_save_args()))
        await handle.wait_for_draft()
        handle.confirm_audible_prefix("The complete", played_ms=100)
        assert await asyncio.gather(first, second) == [(401, 402), (401, 402)]

    immediate.assert_awaited_once()
    transactional_hook.assert_awaited_once()
    assert transactional_hook.await_args.args[0].confirmed_text == "The complete"
    assert transactional_hook.await_args.args[0].played_ms == 100


@pytest.mark.asyncio
async def test_registry_cancellation_is_isolated_by_full_turn_key(monkeypatch):
    immediate = AsyncMock(return_value=(501, 502))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    registry = ChannelTurnRegistry()
    first_context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("same-call", "turn-1"),
    )
    second_context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("same-call", "turn-2"),
    )
    first_handle = await registry.register(first_context)
    second_handle = await registry.register(second_context)

    async def run(context, handle):
        with bind_channel_turn(context, handle):
            handle.bind_owner_task()
            return await persistence.save_content_to_db(*_save_args())

    first = asyncio.create_task(run(first_context, first_handle))
    second = asyncio.create_task(run(second_context, second_handle))
    await asyncio.gather(first_handle.wait_for_draft(), second_handle.wait_for_draft())
    assert await registry.cancel(TurnKey("same-call", "turn-1"), "barge_in")
    second_handle.confirm_audible_prefix("The", played_ms=30)

    with pytest.raises(asyncio.CancelledError):
        await first
    assert await second == (501, 502)
    immediate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prefix", "played_ms"),
    [("Already heard", 240), ("", 0)],
)
async def test_pre_draft_interruption_commits_prefix_or_user_only_once_then_cancels(
    prefix, played_ms
):
    observer = AsyncMock()
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("call-pre-draft", f"turn-{played_ms}"),
        on_commit=observer,
    )
    handle = await ChannelTurnRegistry().register(context)
    fallback = AsyncMock(return_value=(701, 702 if prefix else None))
    handle.register_interruption_fallback(fallback)
    generation_started = asyncio.Event()

    async def unfinished_generation():
        handle.bind_owner_task()
        generation_started.set()
        await asyncio.Event().wait()

    generation = asyncio.create_task(unfinished_generation())
    await generation_started.wait()
    expected = (701, 702 if prefix else None)
    assert await handle.interrupt_and_commit(
        prefix, played_ms=played_ms, reason="barge_in"
    ) == expected
    assert await handle.interrupt_and_commit(
        prefix, played_ms=played_ms, reason="duplicate_mark"
    ) == expected
    fallback.assert_awaited_once()
    observer.assert_awaited_once()
    assert observer.await_args.args[0].confirmed_text == (prefix or None)
    assert observer.await_args.args[0].played_ms == played_ms
    assert fallback.await_args.args[0].text_prefix == prefix
    assert fallback.await_args.args[0].played_ms == played_ms
    with pytest.raises(asyncio.CancelledError):
        await generation


@pytest.mark.asyncio
async def test_interruption_claim_wins_race_against_deferred_publish_and_commit():
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("race-call", "race-turn"),
    )
    handle = await ChannelTurnRegistry().register(context)
    fallback_started = asyncio.Event()
    release_fallback = asyncio.Event()
    normal_commit = AsyncMock(return_value=(801, 802))

    async def fallback(_confirmation, on_database_commit):
        fallback_started.set()
        await release_fallback.wait()
        on_database_commit((801, None))
        return (801, None)

    handle.register_interruption_fallback(fallback)
    interrupt = asyncio.create_task(handle.interrupt_and_commit("", played_ms=0))
    await fallback_started.wait()

    async def provider_reaches_save_late():
        handle.bind_owner_task()
        return await handle.defer_commit("late draft", normal_commit)

    provider = asyncio.create_task(provider_reaches_save_late())
    await asyncio.sleep(0)
    assert handle.draft is None
    release_fallback.set()
    assert await interrupt == (801, None)
    with pytest.raises(asyncio.CancelledError):
        await provider
    normal_commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_draft_interruption_cancels_provider_at_db_commit_but_finishes_side_effects():
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("post-draft", "turn"),
    )
    handle = await ChannelTurnRegistry().register(context)
    draft_ready = asyncio.Event()
    database_committed = asyncio.Event()
    finish_side_effects = asyncio.Event()
    side_effects_done = asyncio.Event()

    async def commit(_confirmation):
        handle.database_commit_completed((811, 812))
        database_committed.set()
        await finish_side_effects.wait()
        side_effects_done.set()
        return (811, 812)

    async def provider():
        handle.bind_owner_task()
        draft_ready.set()
        return await handle.defer_commit("audible answer", commit)

    owner = asyncio.create_task(provider())
    await draft_ready.wait()
    await handle.wait_for_draft()
    interruption = asyncio.create_task(
        handle.interrupt_and_commit("audible", played_ms=150)
    )
    await database_committed.wait()
    assert await interruption == (811, 812)
    with pytest.raises(asyncio.CancelledError):
        await owner
    assert side_effects_done.is_set() is False
    finish_side_effects.set()
    await asyncio.wait_for(handle._persistence_task, timeout=1)
    assert side_effects_done.is_set() is True


@pytest.mark.asyncio
async def test_provider_end_before_draft_closes_handle_and_wakes_waiters():
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("provider-error", "no-draft"),
    )
    registry = ChannelTurnRegistry()
    handle = await registry.register(context)
    waiter = asyncio.create_task(handle.wait_for_draft())
    assert await handle.close_unfinished("provider ended before draft") is True
    with pytest.raises(ChannelPersistenceError, match="provider ended"):
        await waiter
    assert await registry.unregister(handle.key, handle) is True


@pytest.mark.asyncio
async def test_none_message_ids_are_explicit_interruption_failure_and_cancel_owner():
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("failed-persist", "turn"),
    )
    handle = await ChannelTurnRegistry().register(context)
    handle.register_interruption_fallback(AsyncMock(return_value=(None, None)))
    started = asyncio.Event()

    async def generation():
        handle.bind_owner_task()
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(generation())
    await started.wait()
    with pytest.raises(ChannelPersistenceError):
        await handle.interrupt_and_commit("", played_ms=0)
    with pytest.raises(asyncio.CancelledError):
        await owner


def test_ingest_terminal_never_reports_success_without_a_message_id():
    failure = runtime_messages._ingest_terminal_payload(None)
    success = runtime_messages._ingest_terminal_payload(91)

    assert failure["persistence_error"] is True
    assert "terminal" not in failure
    assert success == {"terminal": "queued_for_active_phone", "message_id": 91}


@pytest.mark.asyncio
async def test_ingest_only_returns_user_id_and_provenance_without_bot(monkeypatch):
    immediate = AsyncMock(return_value=(601, None))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    commits = []
    context = ChannelContext(
        channel="device",
        persistence="ingest_only",
        provenance={"origin": "device", "external_id": "msg-9"},
        on_commit=commits.append,
    )

    with bind_channel_turn(context):
        result = await persistence.save_content_to_db(
            *_save_args(), user_message="queued inbound", pending_attachment_refs=["a1"]
        )

    assert result == (601, None)
    assert immediate.await_args.args[0:4] == (None, 0, 0, 0)
    assert immediate.await_args.kwargs["save_assistant_message"] is False
    assert immediate.await_args.kwargs["pending_attachment_refs"] == ["a1"]
    assert immediate.await_args.kwargs["record_user_only_memory"] is False
    assert commits[0].user_message_id == 601
    assert commits[0].assistant_message_id is None
    assert commits[0].context.provenance["external_id"] == "msg-9"


@pytest.mark.asyncio
async def test_phone_ingest_only_records_user_memory_without_assistant_generation(
    monkeypatch,
):
    immediate = AsyncMock(return_value=(602, None))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone",
        persistence="ingest_only",
        turn_key=TurnKey("call-stop", "turn-stop"),
    )

    with bind_channel_turn(context):
        result = await persistence.save_content_to_db(
            *_save_args(), user_message="final stopped phrase"
        )

    assert result == (602, None)
    assert immediate.await_count == 1
    assert immediate.await_args.args[0:4] == (None, 0, 0, 0)
    assert immediate.await_args.kwargs["save_assistant_message"] is False
    assert immediate.await_args.kwargs["persistence_only"] is True
    assert immediate.await_args.kwargs["record_user_only_memory"] is True


@pytest.mark.asyncio
async def test_phone_outbox_ingest_never_waits_for_inline_memory(monkeypatch):
    immediate = AsyncMock(return_value=(603, None))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone",
        persistence="ingest_only",
        turn_key=TurnKey("call-stop", "turn-outbox"),
        provenance={"phone_memory_outbox": True},
    )

    with bind_channel_turn(context):
        result = await persistence.save_content_to_db(
            *_save_args(), user_message="final handed to durable outbox"
        )

    assert result == (603, None)
    assert immediate.await_args.kwargs["record_user_only_memory"] is False
    assert immediate.await_args.kwargs["channel_commit_persistence_only"] is False


@pytest.mark.asyncio
async def test_moderation_interruption_fallback_never_records_memory(monkeypatch):
    immediate = AsyncMock(return_value=(91, None))
    monkeypatch.setattr(persistence, "_save_content_to_db_immediate", immediate)
    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("moderated", "turn"),
    )
    confirmation = SimpleNamespace(text_prefix="", played_ms=0)

    assert await persistence.save_interrupted_channel_turn(
        confirmation,
        context=context,
        conversation_id=41,
        user_id=5,
        model="model",
        user_message="[Blocked Message]",
        persistence_only=True,
    ) == (91, None)
    assert immediate.await_args.kwargs["persistence_only"] is True
    assert immediate.await_args.kwargs["record_user_only_memory"] is False
    assert immediate.await_args.kwargs["load_watchdog_config"] is False


@pytest.mark.asyncio
async def test_persistence_only_skips_billing_reservations_memory_and_watchdog(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "turns.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS(
                id INTEGER PRIMARY KEY, role_id INTEGER, last_activity TEXT
            );
            CREATE TABLE MESSAGES(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message TEXT,
                type TEXT NOT NULL,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                date TEXT,
                llm_id INTEGER,
                citations_json TEXT
            );
            CREATE TABLE MESSAGE_PROVENANCE(
                message_id INTEGER PRIMARY KEY, external_id TEXT NOT NULL
            );
            INSERT INTO CONVERSATIONS(id, role_id) VALUES (41, 9);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def connection_factory(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def write_lock(_conversation_id):
        yield

    consume = AsyncMock(return_value=True)
    prepare = AsyncMock()
    settle = AsyncMock()
    memory = AsyncMock()
    watchdog = Mock(return_value=None)
    finalize = AsyncMock()
    wellbeing = AsyncMock()
    monkeypatch.setattr(persistence, "get_db_connection", connection_factory)
    monkeypatch.setattr(persistence, "conversation_write_lock", write_lock)
    monkeypatch.setattr(persistence, "consume_token", consume)
    monkeypatch.setattr(persistence, "prepare_ai_reservation_settlement", prepare)
    monkeypatch.setattr(persistence, "complete_ai_reservation_settlement", settle)
    monkeypatch.setattr(persistence, "_record_memory_turn_best_effort", memory)
    monkeypatch.setattr(persistence, "_get_post_watchdog_config", watchdog)
    monkeypatch.setattr(persistence, "finalize_message_attachments", finalize)
    monkeypatch.setattr(persistence, "record_chat_turn", wellbeing)
    monkeypatch.setattr(persistence, "discard_pending_attachments", AsyncMock())
    monkeypatch.setattr(
        "chat.services.privacy.is_incognito_conversation",
        AsyncMock(return_value=False),
    )

    transactional_commits = []

    async def link_in_transaction(commit, conn):
        assert conn.in_transaction is True
        transactional_commits.append(commit)
        await conn.execute(
            "INSERT INTO MESSAGE_PROVENANCE(message_id,external_id) VALUES (?,?)",
            (commit.user_message_id, commit.context.provenance["external_id"]),
        )

    ingest_context = ChannelContext(
        channel="device",
        persistence="ingest_only",
        provenance={"external_id": "device-77"},
        on_commit_in_transaction=link_in_transaction,
    )

    with bind_channel_turn(ingest_context):
        result = await persistence.save_content_to_db(
            None,
            999,
            999,
            1998,
            41,
            5,
            "unused-model",
            user_message="queued inbound",
            pending_attachment_refs=["attachment-a"],
        )

    assert result == (1, None)
    assert len(transactional_commits) == 1
    moderated = await persistence._save_content_to_db_immediate(
        "policy rejection",
        0,
        0,
        0,
        41,
        5,
        "unused-model",
        user_message="[Blocked Message]",
        persistence_only=True,
    )
    assert moderated == (2, 3)
    consume.assert_not_awaited()
    prepare.assert_not_awaited()
    settle.assert_not_awaited()
    memory.assert_not_awaited()
    watchdog.assert_not_called()
    wellbeing.assert_awaited_once()
    finalize.assert_awaited_once()
    async with aiosqlite.connect(db_path) as conn:
        rows = await (await conn.execute(
            "SELECT message,type FROM MESSAGES ORDER BY id"
        )).fetchall()
        provenance = await (await conn.execute(
            "SELECT message_id,external_id FROM MESSAGE_PROVENANCE"
        )).fetchall()
    assert rows == [
        ("queued inbound", "user"),
        ("[Blocked Message]", "user"),
        ("policy rejection", "bot"),
    ]
    assert provenance == [(1, "device-77")]

    user_only = await persistence._save_content_to_db_immediate(
        None,
        0,
        0,
        0,
        41,
        5,
        "unused-model",
        user_message="heard no assistant audio",
        save_assistant_message=False,
        skip_billing=True,
        record_user_only_memory=True,
    )
    assert user_only == (4, None)
    memory.assert_awaited_once()
    assert memory.await_args.kwargs["assistant_content"] is None
    assert memory.await_args.kwargs["assistant_message_id"] is None


@pytest.mark.asyncio
async def test_real_normal_deferred_commit_bills_memorizes_and_enqueues_watchdog_once(
    monkeypatch, tmp_path
):
    db_path = tmp_path / "real-deferred.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS(
                id INTEGER PRIMARY KEY, role_id INTEGER, last_activity TEXT
            );
            CREATE TABLE MESSAGES(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message TEXT,
                type TEXT NOT NULL,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                date TEXT,
                llm_id INTEGER,
                citations_json TEXT
            );
            CREATE TABLE LLM(
                id INTEGER PRIMARY KEY, model TEXT,
                input_token_cost REAL, output_token_cost REAL
            );
            INSERT INTO CONVERSATIONS(id, role_id) VALUES (41, 9);
            INSERT INTO LLM(id,model,input_token_cost,output_token_cost)
            VALUES (7,'real-model',1.0,2.0);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def connection_factory(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def write_lock(_conversation_id):
        yield

    consume = AsyncMock(return_value=True)
    memory = AsyncMock(return_value=True)
    watchdog_send = Mock()
    finalize_attachments = AsyncMock()
    monkeypatch.setattr(persistence, "get_db_connection", connection_factory)
    monkeypatch.setattr(persistence, "conversation_write_lock", write_lock)
    monkeypatch.setattr(persistence, "consume_token", consume)
    monkeypatch.setattr(
        persistence, "prepare_ai_reservation_settlement", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        persistence, "complete_ai_reservation_settlement", AsyncMock()
    )
    monkeypatch.setattr(persistence, "_record_memory_turn_best_effort", memory)
    monkeypatch.setattr(
        persistence, "_get_post_watchdog_config", Mock(return_value={"enabled": True})
    )
    monkeypatch.setattr(persistence, "record_chat_turn", AsyncMock())
    monkeypatch.setattr(
        persistence, "finalize_message_attachments", finalize_attachments
    )
    monkeypatch.setattr(persistence, "discard_pending_attachments", AsyncMock())
    monkeypatch.setattr(
        "chat.services.privacy.is_incognito_conversation",
        AsyncMock(return_value=False),
    )
    import tools.watchdog as watchdog_module

    monkeypatch.setattr(
        watchdog_module, "watchdog_evaluate_task", SimpleNamespace(send=watchdog_send)
    )

    context = ChannelContext(
        channel="phone", persistence="deferred",
        turn_key=TurnKey("real-call", "real-turn"),
    )
    handle = await ChannelTurnRegistry().register(context)
    kwargs = dict(
        user_message="canonical user",
        prompt_id=9,
        watchdog_config={"enabled": True},
        llm_id=7,
        pending_attachment_refs=["pending-phone-audio"],
    )
    with bind_channel_turn(context, handle):
        first = asyncio.create_task(
            persistence.save_content_to_db(
                "canonical assistant", 11, 5, 16, 41, 5, "real-model", **kwargs
            )
        )
        second = asyncio.create_task(
            persistence.save_content_to_db(
                "canonical assistant", 11, 5, 16, 41, 5, "real-model", **kwargs
            )
        )
        await handle.wait_for_draft()
        handle.confirm_audible_prefix("canonical assistant", played_ms=700)
        assert await asyncio.gather(first, second) == [(1, 2), (1, 2)]

    consume.assert_awaited_once()
    memory.assert_awaited_once()
    finalize_attachments.assert_awaited_once()
    watchdog_send.assert_called_once_with(41, 1, 2, 9)
    async with aiosqlite.connect(db_path) as conn:
        rows = await (await conn.execute(
            "SELECT message,type FROM MESSAGES ORDER BY id"
        )).fetchall()
    assert rows == [
        ("canonical user", "user"),
        ("canonical assistant", "bot"),
    ]


@pytest.mark.asyncio
async def test_transactional_provenance_failure_rolls_back_messages(monkeypatch, tmp_path):
    db_path = tmp_path / "provenance-rollback.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS(
                id INTEGER PRIMARY KEY, role_id INTEGER, last_activity TEXT
            );
            CREATE TABLE MESSAGES(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message TEXT,
                type TEXT NOT NULL,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                date TEXT,
                llm_id INTEGER,
                citations_json TEXT
            );
            INSERT INTO CONVERSATIONS(id, role_id) VALUES (41, 9);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def connection_factory(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def write_lock(_conversation_id):
        yield

    async def fail_link(_commit, conn):
        assert conn.in_transaction is True
        raise RuntimeError("link failed")

    post_commit = AsyncMock()
    context = ChannelContext(
        channel="device",
        persistence="ingest_only",
        on_commit_in_transaction=fail_link,
        on_commit=post_commit,
    )
    monkeypatch.setattr(persistence, "get_db_connection", connection_factory)
    monkeypatch.setattr(persistence, "conversation_write_lock", write_lock)
    monkeypatch.setattr(persistence, "discard_pending_attachments", AsyncMock())
    monkeypatch.setattr(
        "chat.services.privacy.is_incognito_conversation",
        AsyncMock(return_value=False),
    )

    with bind_channel_turn(context):
        assert await persistence.save_content_to_db(
            None, 0, 0, 0, 41, 5, "unused-model", user_message="rollback me"
        ) == (None, None)

    post_commit.assert_not_awaited()
    async with aiosqlite.connect(db_path) as conn:
        count = (await (await conn.execute("SELECT COUNT(*) FROM MESSAGES")).fetchone())[0]
    assert count == 0


@pytest.mark.asyncio
async def test_transactional_guard_rejects_before_any_insert(monkeypatch, tmp_path):
    db_path = tmp_path / "guard.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.executescript(
            """
            CREATE TABLE CONVERSATIONS(
                id INTEGER PRIMARY KEY, role_id INTEGER, last_activity TEXT
            );
            CREATE TABLE MESSAGES(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                message TEXT,
                type TEXT NOT NULL,
                input_tokens_used INTEGER,
                output_tokens_used INTEGER,
                date TEXT,
                llm_id INTEGER,
                citations_json TEXT
            );
            INSERT INTO CONVERSATIONS(id, role_id) VALUES (41, 9);
            """
        )
        await conn.commit()

    @asynccontextmanager
    async def connection_factory(readonly=False):
        conn = await aiosqlite.connect(db_path)
        conn.row_factory = aiosqlite.Row
        try:
            yield conn
        finally:
            await conn.close()

    @asynccontextmanager
    async def write_lock(_conversation_id):
        yield

    guard_connections = []

    async def guard(_context, conn):
        guard_connections.append(conn)
        # The write transaction is already active when fencing is checked.
        assert conn.in_transaction is True
        return False

    context = ChannelContext(
        channel="device",
        persistence="immediate",
        turn_key=TurnKey("foreground", "device-message"),
        commit_guard=guard,
    )
    monkeypatch.setattr(persistence, "get_db_connection", connection_factory)
    monkeypatch.setattr(persistence, "conversation_write_lock", write_lock)
    monkeypatch.setattr(persistence, "discard_pending_attachments", AsyncMock())
    monkeypatch.setattr(
        "chat.services.privacy.is_incognito_conversation",
        AsyncMock(return_value=False),
    )

    with bind_channel_turn(context):
        with pytest.raises(StaleChannelTurnError):
            await persistence.save_content_to_db(
                *_save_args(), user_message="must not persist"
            )

    assert len(guard_connections) == 1
    async with aiosqlite.connect(db_path) as conn:
        count = (await (await conn.execute("SELECT COUNT(*) FROM MESSAGES")).fetchone())[0]
    assert count == 0
