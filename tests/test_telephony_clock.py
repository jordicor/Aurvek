from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ai_runtime.watchdog.prompting import prepare_pre_watchdog_and_model_prompt
from integrations.telephony.clock import (
    CallClockMode,
    CallEndController,
    DeterministicCallClock,
    DeterministicSilenceWatchdog,
    EndCallDirective,
    EndCallKind,
    EndCallReason,
    MilestoneRole,
    SilenceDirectiveKind,
    assign_milestone_roles,
    build_phone_pre_watchdog_context,
)
from integrations.telephony.config import TelephonyConfig
from integrations.telephony.settings import resolve_phone_settings


START = datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, current: datetime = START) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _settings(**changes):
    base = resolve_phone_settings(TelephonyConfig(max_call_seconds=1200), None)
    return replace(base, **changes)


def test_clock_reports_elapsed_remaining_mode_and_deadline():
    now = FakeClock()
    clock = DeterministicCallClock(_settings(), now=now)

    initial = clock.at_safe_point()
    now.advance(201)
    later = clock.at_safe_point()

    assert initial.elapsed_seconds == 0
    assert initial.remaining_seconds == 1200
    assert initial.mode == CallClockMode.NORMAL
    assert later.elapsed_seconds == 201
    assert later.remaining_seconds == 999
    assert later.deadline_at == START + timedelta(seconds=1200)


def test_crossing_multiple_milestones_emits_once_with_most_restrictive_mode():
    now = FakeClock()
    clock = DeterministicCallClock(_settings(), now=now)
    clock.at_safe_point()

    now.advance(1030)  # 170s remain: crosses 900, 300 and 180 at once.
    crossed = clock.at_safe_point()
    repeated = clock.at_safe_point()

    assert crossed.mode == CallClockMode.CLOSING
    assert crossed.safe_point_directive is not None
    assert crossed.safe_point_directive.crossed_milestones_seconds == (900, 300, 180)
    assert crossed.safe_point_directive.crossed_milestone_roles == (
        MilestoneRole.START_WRAP_UP,
        MilestoneRole.FINISH_CURRENT_TOPIC,
        MilestoneRole.NO_NEW_TOPICS,
    )
    assert "Do not open new topics" in crossed.safe_point_directive.instruction
    assert repeated.safe_point_directive is None
    assert clock.fired_milestones_seconds == (900, 300, 180)

    now.advance(111)  # 59s remain: crosses the final milestone once.
    final_warning = clock.at_safe_point()
    assert final_warning.safe_point_directive is not None
    assert final_warning.safe_point_directive.crossed_milestones_seconds == (60,)
    assert clock.at_safe_point().safe_point_directive is None


def test_deadline_poll_does_not_consume_milestone_before_llm_safe_point():
    now = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    clock = DeterministicCallClock(_settings(), now=now)
    clock.at_safe_point()
    now.advance(301)  # Cross the 15-minute threshold.

    timer_tick = clock.poll_deadline()
    llm_tick = clock.peek_safe_point()

    assert timer_tick.safe_point_directive is not None
    assert llm_tick.safe_point_directive is not None
    assert llm_tick.safe_point_directive.crossed_milestones_seconds == (900,)
    assert clock.fired_milestones_seconds == ()

    clock.acknowledge_milestones((900,))
    assert clock.peek_safe_point().safe_point_directive is None


def test_reanchor_uses_answer_time_and_restores_previous_stream_milestones():
    now = FakeClock(datetime(2026, 1, 1, 12, 30, tzinfo=timezone.utc))
    clock = DeterministicCallClock(
        _settings(),
        started_at=datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc),
        now=now,
    )

    clock.reanchor(datetime(2026, 1, 1, 12, 20, tzinfo=timezone.utc))
    clock.restore_fired_milestones((900,))
    tick = clock.peek_safe_point()

    assert tick.elapsed_seconds == 600
    assert tick.remaining_seconds == 600
    assert tick.safe_point_directive is None


def test_custom_milestones_derive_semantics_only_from_position():
    now = FakeClock()
    clock = DeterministicCallClock(
        _settings(warning_milestones_seconds=(600, 240, 120, 30)),
        now=now,
    )
    expected = (
        (601, 599, MilestoneRole.START_WRAP_UP, CallClockMode.WRAP_UP),
        (360, 239, MilestoneRole.FINISH_CURRENT_TOPIC, CallClockMode.WRAP_UP),
        (120, 119, MilestoneRole.NO_NEW_TOPICS, CallClockMode.CLOSING),
        (90, 29, MilestoneRole.FINAL_TURN, CallClockMode.CLOSING),
    )

    for advance, remaining, role, mode in expected:
        now.advance(advance)
        tick = clock.at_safe_point()
        assert tick.remaining_seconds == remaining
        assert tick.mode == mode
        assert tick.safe_point_directive.crossed_milestone_roles == (role,)


def test_fewer_and_extra_milestones_have_monotonic_positional_roles():
    assert assign_milestone_roles((600,)) == (
        (600, MilestoneRole.START_WRAP_UP),
    )
    assert assign_milestone_roles((600, 60)) == (
        (600, MilestoneRole.START_WRAP_UP),
        (60, MilestoneRole.FINAL_TURN),
    )
    roles = tuple(
        role
        for _, role in assign_milestone_roles((900, 600, 300, 120, 30))
    )
    assert roles == (
        MilestoneRole.START_WRAP_UP,
        MilestoneRole.FINISH_CURRENT_TOPIC,
        MilestoneRole.NO_NEW_TOPICS,
        MilestoneRole.NO_NEW_TOPICS,
        MilestoneRole.FINAL_TURN,
    )


def test_deadline_forces_non_cancelable_close_once():
    now = FakeClock()
    clock = DeterministicCallClock(_settings(), now=now)
    now.advance(1200)

    deadline = clock.at_safe_point()
    repeated = clock.at_safe_point()

    assert deadline.remaining_seconds == 0
    assert deadline.mode == CallClockMode.CLOSING
    assert deadline.end_call is not None
    assert deadline.end_call.kind == EndCallKind.FORCED
    assert deadline.end_call.reason == EndCallReason.DEADLINE
    assert deadline.end_call.cancelable_by_real_speech is False
    assert repeated.end_call is None
    assert repeated.safe_point_directive is None


def test_phone_context_is_identical_for_pre_watchdog_and_main_model():
    tick = DeterministicCallClock(_settings(), started_at=START, now=FakeClock()).at_safe_point()

    context = build_phone_pre_watchdog_context(
        tick,
        pre_watchdog_directive="[WATCHDOG] Keep the response concise.",
    )

    assert context.phone_internal_block in context.pre_watchdog_input
    assert context.phone_internal_block in context.assistant_internal_context
    assert "remaining_seconds=1200" in context.phone_internal_block
    assert "mode=normal" in context.phone_internal_block
    assert "Keep the response concise" in context.assistant_internal_context
    assert "Keep the response concise" not in context.pre_watchdog_input

    model_prompt, pre_watchdog_prompt = prepare_pre_watchdog_and_model_prompt(
        "Base prompt",
        context.phone_internal_block,
    )
    assert model_prompt == pre_watchdog_prompt
    assert context.phone_internal_block in model_prompt


def test_phone_context_treats_unanswered_caller_messages_as_evolving_intent():
    tick = DeterministicCallClock(
        _settings(), started_at=START, now=FakeClock()
    ).at_safe_point()

    context = build_phone_pre_watchdog_context(tick)

    for target in (
        context.phone_internal_block,
        context.pre_watchdog_input,
        context.assistant_internal_context,
    ):
        assert "consecutive participant messages" in target
        assert "no audible assistant reply" in target
        assert "only where they contradict" in target
        assert "clearly revise or cancel" in target
        assert "preserve independent compatible requests" in target
        assert "phone clock, watchdog, and safety directives" in target
        assert "combined current intent" in target
        assert "clearly superseded" in target
        assert "Twilio" not in target
        assert "ElevenLabs" not in target
        assert "AVA" not in target


def test_optional_pre_watchdog_adapter_does_not_change_normal_channels():
    model_prompt, pre_watchdog_prompt = prepare_pre_watchdog_and_model_prompt(
        "Base prompt",
        None,
    )
    assert model_prompt == "Base prompt"
    assert pre_watchdog_prompt == "Base prompt"


def test_silence_watchdog_uses_two_stages_without_repetition():
    now = FakeClock()
    watchdog = DeterministicSilenceWatchdog(
        _settings(silence_prompt_seconds=60, silence_hangup_seconds=60),
        now=now,
    )

    now.advance(59)
    assert watchdog.at_safe_point() is None
    now.advance(1)
    check = watchdog.at_safe_point()
    assert check is not None
    assert check.kind == SilenceDirectiveKind.CHECK_PRESENCE
    assert watchdog.at_safe_point() is None

    # TTS/queue delay does not consume the participant's second 60 seconds.
    now.advance(30)
    assert watchdog.at_safe_point() is None
    assert watchdog.confirm_presence_check_audible() is True
    assert watchdog.confirm_presence_check_audible() is False

    now.advance(59)
    assert watchdog.at_safe_point() is None
    now.advance(1)
    close = watchdog.at_safe_point()
    assert close is not None
    assert close.kind == SilenceDirectiveKind.FORCED_CLOSE
    assert close.end_call is not None
    assert close.end_call.kind == EndCallKind.FORCED
    assert close.end_call.reason == EndCallReason.SILENCE
    assert watchdog.at_safe_point() is None


def test_real_speech_resets_presence_check_but_not_emitted_forced_close():
    now = FakeClock()
    watchdog = DeterministicSilenceWatchdog(
        _settings(silence_prompt_seconds=60, silence_hangup_seconds=60),
        now=now,
    )
    now.advance(60)
    assert watchdog.at_safe_point().kind == SilenceDirectiveKind.CHECK_PRESENCE

    now.advance(30)
    watchdog.on_real_participant_speech()
    now.advance(59)
    assert watchdog.at_safe_point() is None
    now.advance(1)
    assert watchdog.at_safe_point().kind == SilenceDirectiveKind.CHECK_PRESENCE

    assert watchdog.confirm_presence_check_audible() is True
    now.advance(60)
    assert watchdog.at_safe_point().kind == SilenceDirectiveKind.FORCED_CLOSE
    watchdog.on_real_participant_speech()
    assert watchdog.at_safe_point() is None


def test_disabled_silence_watchdog_never_emits():
    now = FakeClock()
    watchdog = DeterministicSilenceWatchdog(
        _settings(silence_prompt_seconds=None, silence_hangup_seconds=None),
        now=now,
    )
    now.advance(10_000)
    assert watchdog.enabled is False
    assert watchdog.at_safe_point() is None


def test_voluntary_end_call_is_cancelable_by_real_speech():
    controller = CallEndController()
    voluntary = EndCallDirective.voluntary(
        requested_at=START,
        final_message="Thanks, goodbye.",
    )
    controller.request(voluntary)

    assert voluntary.cancelable_by_real_speech is True
    assert controller.on_real_participant_speech() is True
    assert controller.pending is None
    assert controller.audio_confirmed() is None


def test_voluntary_end_call_rejects_empty_final_message():
    with pytest.raises(ValueError, match="non-empty final_message"):
        EndCallDirective.voluntary(
            requested_at=START,
            final_message="   ",
        )


def test_forced_end_call_cannot_be_canceled_or_weakened():
    controller = CallEndController()
    forced = EndCallDirective.forced(
        requested_at=START,
        reason=EndCallReason.BALANCE,
        audible_notice="The call must end.",
    )
    controller.request(forced)

    assert controller.on_real_participant_speech() is False
    assert controller.request(
        EndCallDirective.voluntary(
            requested_at=START,
            final_message="Maybe goodbye.",
        )
    ) is forced
    assert controller.audio_confirmed() is forced
    assert controller.pending is None
