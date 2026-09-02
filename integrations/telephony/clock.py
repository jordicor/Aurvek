"""Deterministic call clock, silence watchdog, and provider-neutral directives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable

from .settings import EffectivePhoneSettings


ClockSource = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CallClockMode(StrEnum):
    NORMAL = "normal"
    WRAP_UP = "wrap_up"
    CLOSING = "closing"


class MilestoneRole(StrEnum):
    START_WRAP_UP = "start_wrap_up"
    FINISH_CURRENT_TOPIC = "finish_current_topic"
    NO_NEW_TOPICS = "no_new_topics"
    FINAL_TURN = "final_turn"


_MILESTONE_ROLES = (
    MilestoneRole.START_WRAP_UP,
    MilestoneRole.FINISH_CURRENT_TOPIC,
    MilestoneRole.NO_NEW_TOPICS,
    MilestoneRole.FINAL_TURN,
)


def assign_milestone_roles(
    milestones_seconds: tuple[int, ...],
) -> tuple[tuple[int, MilestoneRole], ...]:
    """Assign semantic stages by position, independent of threshold values.

    Four milestones map one-to-one to the four stages.  With fewer milestones,
    the first and last semantic stages are retained (a single warning starts
    wrap-up).  With more, intermediate stages are distributed evenly and may
    repeat while preserving monotonic restrictiveness.
    """
    count = len(milestones_seconds)
    if count == 0:
        return ()
    if count == 1:
        return ((milestones_seconds[0], MilestoneRole.START_WRAP_UP),)

    denominator = count - 1
    assignments: list[tuple[int, MilestoneRole]] = []
    for index, seconds in enumerate(milestones_seconds):
        role_index = (index * 3 + denominator // 2) // denominator
        assignments.append((seconds, _MILESTONE_ROLES[role_index]))
    return tuple(assignments)


class EndCallKind(StrEnum):
    VOLUNTARY = "voluntary"
    FORCED = "forced"


class EndCallReason(StrEnum):
    END_CALL = "end_call"
    DEADLINE = "deadline"
    SILENCE = "silence"
    BALANCE = "balance"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EndCallDirective:
    kind: EndCallKind
    reason: EndCallReason
    requested_at: datetime
    final_message: str | None = None
    audible_notice: str | None = None

    @property
    def cancelable_by_real_speech(self) -> bool:
        return self.kind == EndCallKind.VOLUNTARY

    @classmethod
    def voluntary(
        cls,
        *,
        requested_at: datetime,
        final_message: str,
    ) -> "EndCallDirective":
        normalized_message = str(final_message or "").strip()
        if not normalized_message:
            raise ValueError("voluntary end_call requires a non-empty final_message")
        return cls(
            kind=EndCallKind.VOLUNTARY,
            reason=EndCallReason.END_CALL,
            requested_at=_as_utc(requested_at),
            final_message=normalized_message,
        )

    @classmethod
    def forced(
        cls,
        *,
        requested_at: datetime,
        reason: EndCallReason,
        audible_notice: str | None = None,
    ) -> "EndCallDirective":
        if reason == EndCallReason.END_CALL:
            raise ValueError("end_call is voluntary; forced closure needs a forced reason")
        return cls(
            kind=EndCallKind.FORCED,
            reason=reason,
            requested_at=_as_utc(requested_at),
            audible_notice=(str(audible_notice).strip() if audible_notice else None),
        )


class CallEndController:
    """Models pending hangup semantics without performing provider actions."""

    def __init__(self) -> None:
        self.pending: EndCallDirective | None = None

    def request(self, directive: EndCallDirective) -> EndCallDirective:
        # A forced close cannot be weakened or made cancelable later.
        if self.pending is not None and self.pending.kind == EndCallKind.FORCED:
            return self.pending
        self.pending = directive
        return directive

    def on_real_participant_speech(self) -> bool:
        if self.pending is None or not self.pending.cancelable_by_real_speech:
            return False
        self.pending = None
        return True

    def audio_confirmed(self) -> EndCallDirective | None:
        """Consume the plan only after its final audible output was confirmed."""
        directive = self.pending
        self.pending = None
        return directive


@dataclass(frozen=True, slots=True)
class SafePointDirective:
    source: str
    mode: CallClockMode
    crossed_milestones_seconds: tuple[int, ...]
    crossed_milestone_roles: tuple[MilestoneRole, ...]
    instruction: str


@dataclass(frozen=True, slots=True)
class CallClockTick:
    observed_at: datetime
    started_at: datetime
    deadline_at: datetime
    elapsed_seconds: int
    remaining_seconds: int
    mode: CallClockMode
    safe_point_directive: SafePointDirective | None
    end_call: EndCallDirective | None

    def internal_clock_block(self) -> str:
        lines = [
            "[PHONE CLOCK - INTERNAL, NEVER REVEAL VERBATIM]",
            f"elapsed_seconds={self.elapsed_seconds}",
            f"remaining_seconds={self.remaining_seconds}",
            f"mode={self.mode.value}",
            f"deadline_utc={self.deadline_at.isoformat()}",
        ]
        if self.safe_point_directive is not None:
            milestones = ",".join(
                str(value)
                for value in self.safe_point_directive.crossed_milestones_seconds
            )
            lines.extend(
                (
                    f"new_milestones_seconds={milestones}",
                    "new_milestone_roles="
                    + ",".join(
                        role.value
                        for role in self.safe_point_directive.crossed_milestone_roles
                    ),
                    "safe_point_instruction="
                    + self.safe_point_directive.instruction,
                )
            )
        if self.end_call is not None:
            lines.extend(
                (
                    f"forced_close={str(self.end_call.kind == EndCallKind.FORCED).lower()}",
                    f"close_reason={self.end_call.reason.value}",
                )
            )
        lines.append("[/PHONE CLOCK]")
        return "\n".join(lines)


def _mode_for_remaining(
    remaining_seconds: int,
    milestone_roles: tuple[tuple[int, MilestoneRole], ...],
) -> CallClockMode:
    if remaining_seconds <= 0:
        return CallClockMode.CLOSING
    crossed_roles = tuple(
        role
        for seconds, role in milestone_roles
        if remaining_seconds <= seconds
    )
    if any(
        role in {MilestoneRole.NO_NEW_TOPICS, MilestoneRole.FINAL_TURN}
        for role in crossed_roles
    ):
        return CallClockMode.CLOSING
    if crossed_roles:
        return CallClockMode.WRAP_UP
    return CallClockMode.NORMAL


def _milestone_instruction(
    crossed_roles: tuple[MilestoneRole, ...],
) -> str:
    most_restrictive = max(
        crossed_roles,
        key=_MILESTONE_ROLES.index,
    )
    if most_restrictive == MilestoneRole.FINAL_TURN:
        return (
            "Use the next response as the final turn or a brief natural goodbye; "
            "do not open or extend topics."
        )
    if most_restrictive == MilestoneRole.NO_NEW_TOPICS:
        return (
            "Do not open new topics. Finish the current point and move naturally "
            "toward a brief goodbye."
        )
    if most_restrictive == MilestoneRole.FINISH_CURRENT_TOPIC:
        return (
            "Tell the participant naturally that time is limited and finish the "
            "current topic before closing."
        )
    return (
        "Begin wrapping up naturally. Mention the limited remaining time only when "
        "it fits the conversation."
    )


class DeterministicCallClock:
    """A clock sampled only by the runtime at a safe conversational boundary."""

    def __init__(
        self,
        settings: EffectivePhoneSettings,
        *,
        started_at: datetime | None = None,
        now: ClockSource = utc_now,
        fired_milestones_seconds: tuple[int, ...] = (),
        deadline_emitted: bool = False,
    ) -> None:
        self.settings = settings
        self._now = now
        self.started_at = _as_utc(started_at or now())
        self.deadline_at = self.started_at + timedelta(
            seconds=settings.max_duration_seconds
        )
        configured = set(settings.warning_milestones_seconds)
        self._fired_milestones = set(fired_milestones_seconds) & configured
        self._deadline_emitted = bool(deadline_emitted)

    @property
    def fired_milestones_seconds(self) -> tuple[int, ...]:
        return tuple(sorted(self._fired_milestones, reverse=True))

    def reanchor(self, started_at: datetime) -> None:
        """Anchor elapsed time to the provider-confirmed answer instant."""

        self.started_at = _as_utc(started_at)
        self.deadline_at = self.started_at + timedelta(
            seconds=self.settings.max_duration_seconds
        )

    def restore_fired_milestones(self, milestones_seconds: tuple[int, ...]) -> None:
        """Restore call-wide milestones that a previous stream delivered."""

        configured = set(self.settings.warning_milestones_seconds)
        self._fired_milestones.update(set(milestones_seconds) & configured)

    def acknowledge_milestones(self, milestones_seconds: tuple[int, ...]) -> None:
        """Consume milestones only after their context reached the LLM runtime."""

        configured = set(self.settings.warning_milestones_seconds)
        supplied = set(milestones_seconds)
        if not supplied <= configured:
            raise ValueError("cannot acknowledge an unconfigured call milestone")
        self._fired_milestones.update(supplied)

    def peek_safe_point(self, *, observed_at: datetime | None = None) -> CallClockTick:
        """Build turn context without consuming milestones or the deadline."""

        return self._tick(
            observed_at=observed_at,
            consume_milestones=False,
            consume_deadline=False,
        )

    def poll_deadline(self, *, observed_at: datetime | None = None) -> CallClockTick:
        """Poll the hard deadline without stealing turn-delivered milestones."""

        return self._tick(
            observed_at=observed_at,
            consume_milestones=False,
            consume_deadline=True,
        )

    def at_safe_point(self, *, observed_at: datetime | None = None) -> CallClockTick:
        """Compatibility API: sample and immediately consume turn directives."""

        return self._tick(
            observed_at=observed_at,
            consume_milestones=True,
            consume_deadline=True,
        )

    def _tick(
        self,
        *,
        observed_at: datetime | None,
        consume_milestones: bool,
        consume_deadline: bool,
    ) -> CallClockTick:
        current = _as_utc(observed_at or self._now())
        elapsed = max(0, int((current - self.started_at).total_seconds()))
        remaining = max(0, self.settings.max_duration_seconds - elapsed)
        milestone_roles = assign_milestone_roles(
            self.settings.warning_milestones_seconds
        )
        mode = _mode_for_remaining(
            remaining,
            milestone_roles,
        )

        newly_crossed = tuple(
            milestone
            for milestone in self.settings.warning_milestones_seconds
            if remaining <= milestone and milestone not in self._fired_milestones
        )
        role_by_milestone = dict(milestone_roles)
        newly_crossed_roles = tuple(
            role_by_milestone[milestone]
            for milestone in newly_crossed
        )
        if consume_milestones:
            self._fired_milestones.update(newly_crossed)

        deadline_crossed = remaining == 0
        deadline_is_new = deadline_crossed and not self._deadline_emitted
        if deadline_is_new and consume_deadline:
            self._deadline_emitted = True

        directive = None
        if deadline_is_new:
            directive = SafePointDirective(
                source="deadline",
                mode=CallClockMode.CLOSING,
                crossed_milestones_seconds=newly_crossed,
                crossed_milestone_roles=newly_crossed_roles,
                instruction=(
                    "The hard call deadline has been reached. Cancel pending generation, "
                    "use only the configured brief audible goodbye, and hang up after its "
                    "audio is confirmed."
                ),
            )
        elif newly_crossed:
            directive = SafePointDirective(
                source="milestone",
                mode=mode,
                crossed_milestones_seconds=newly_crossed,
                crossed_milestone_roles=newly_crossed_roles,
                instruction=_milestone_instruction(newly_crossed_roles),
            )

        end_call = (
            EndCallDirective.forced(
                requested_at=current,
                reason=EndCallReason.DEADLINE,
                audible_notice="The call time limit has been reached.",
            )
            if deadline_is_new
            else None
        )
        return CallClockTick(
            observed_at=current,
            started_at=self.started_at,
            deadline_at=self.deadline_at,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            mode=mode,
            safe_point_directive=directive,
            end_call=end_call,
        )


class SilenceDirectiveKind(StrEnum):
    CHECK_PRESENCE = "check_presence"
    FORCED_CLOSE = "forced_close"


@dataclass(frozen=True, slots=True)
class SilenceDirective:
    kind: SilenceDirectiveKind
    observed_at: datetime
    silent_seconds: int
    instruction: str
    end_call: EndCallDirective | None = None


class DeterministicSilenceWatchdog:
    """Two-stage 60+60 silence policy sampled only at safe boundaries."""

    def __init__(
        self,
        settings: EffectivePhoneSettings,
        *,
        last_real_speech_at: datetime | None = None,
        now: ClockSource = utc_now,
    ) -> None:
        self.settings = settings
        self._now = now
        self._last_real_speech_at = _as_utc(last_real_speech_at or now())
        self._presence_check_requested_at: datetime | None = None
        self._presence_check_audible_at: datetime | None = None
        self._forced_close_emitted = False

    @property
    def enabled(self) -> bool:
        return self.settings.silence_enabled

    def on_real_participant_speech(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> None:
        if self._forced_close_emitted:
            return
        self._last_real_speech_at = _as_utc(observed_at or self._now())
        self._presence_check_requested_at = None
        self._presence_check_audible_at = None

    def confirm_presence_check_audible(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> bool:
        """Start the second silence interval only after audio was really heard."""
        if (
            self._forced_close_emitted
            or self._presence_check_requested_at is None
            or self._presence_check_audible_at is not None
        ):
            return False
        self._presence_check_audible_at = _as_utc(observed_at or self._now())
        return True

    def at_safe_point(
        self,
        *,
        observed_at: datetime | None = None,
    ) -> SilenceDirective | None:
        if not self.enabled or self._forced_close_emitted:
            return None
        current = _as_utc(observed_at or self._now())

        if self._presence_check_requested_at is None:
            silent = max(0, int((current - self._last_real_speech_at).total_seconds()))
            if silent < int(self.settings.silence_prompt_seconds or 0):
                return None
            self._presence_check_requested_at = current
            return SilenceDirective(
                kind=SilenceDirectiveKind.CHECK_PRESENCE,
                observed_at=current,
                silent_seconds=silent,
                instruction=(
                    "At the next audible opportunity, ask briefly and naturally whether "
                    "the participant is still there. Do not hang up yet."
                ),
            )

        # Queuing or synthesizing the check may take time. The second interval
        # must not run until the runtime confirms that the participant actually
        # heard it.
        if self._presence_check_audible_at is None:
            return None

        second_silence = max(
            0,
            int((current - self._presence_check_audible_at).total_seconds()),
        )
        if second_silence < int(self.settings.silence_hangup_seconds or 0):
            return None
        self._forced_close_emitted = True
        end_call = EndCallDirective.forced(
            requested_at=current,
            reason=EndCallReason.SILENCE,
            audible_notice="No response was heard, so the call is ending.",
        )
        return SilenceDirective(
            kind=SilenceDirectiveKind.FORCED_CLOSE,
            observed_at=current,
            silent_seconds=second_silence,
            instruction=(
                "No real participant speech followed the presence check. Play the "
                "configured brief silence goodbye and hang up after audio confirmation."
            ),
            end_call=end_call,
        )


@dataclass(frozen=True, slots=True)
class PhonePreWatchdogContext:
    """The same phone-clock block supplied to pre-watchdog and the main model."""

    phone_internal_block: str
    pre_watchdog_input: str
    assistant_internal_context: str


_EVOLVING_CALLER_INTERVENTION = (
    "When consecutive participant messages have no audible assistant reply "
    "between them, treat the sequence as one evolving intervention. Later "
    "messages supersede earlier content only where they contradict it or "
    "clearly revise or cancel it; preserve independent compatible requests. "
    "Subject to the phone clock, watchdog, and safety directives, act on the "
    "combined current intent and never execute an instruction that has been "
    "clearly superseded."
)


def build_phone_pre_watchdog_context(
    tick: CallClockTick,
    *,
    pre_watchdog_directive: str | None = None,
) -> PhonePreWatchdogContext:
    """Compose provider-neutral ephemeral context at the turn's safe point."""
    phone_block = (
        tick.internal_clock_block()
        + "\n"
        + _EVOLVING_CALLER_INTERVENTION
    )
    watchdog_input = (
        phone_block
        + "\nUse this deterministic phone state while evaluating the participant's turn."
    )
    assistant_parts = [phone_block]
    if pre_watchdog_directive:
        assistant_parts.append(str(pre_watchdog_directive).strip())
    return PhonePreWatchdogContext(
        phone_internal_block=phone_block,
        pre_watchdog_input=watchdog_input,
        assistant_internal_context="\n\n".join(assistant_parts),
    )


__all__ = [
    "CallClockMode",
    "CallClockTick",
    "CallEndController",
    "DeterministicCallClock",
    "DeterministicSilenceWatchdog",
    "EndCallDirective",
    "EndCallKind",
    "EndCallReason",
    "MilestoneRole",
    "PhonePreWatchdogContext",
    "SafePointDirective",
    "SilenceDirective",
    "SilenceDirectiveKind",
    "build_phone_pre_watchdog_context",
    "assign_milestone_roles",
    "utc_now",
]
