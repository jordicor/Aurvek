"""Deferred phone-turn contexts for the canonical conversational runtime.

The media transport owns playback confirmation, while ``ai_runtime`` owns
generation, billing, persistence, watchdog and memory.  This module is the
small bridge between both domains: it fences every commit to the live phone
lease and links the resulting canonical ``MESSAGES`` rows to the durable call
inside the same SQLite transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

from ai_runtime.channel_turns import (
    ChannelCommit,
    ChannelContext,
    PhoneDirection,
    PhoneRequestSource,
    PhoneRequestTiming,
    StaleChannelTurnError,
    TurnKey,
)
from integrations.telephony.foreground import (
    ForegroundCommitGuard,
    assert_commit_guard_in_transaction,
)
from integrations.telephony.clock import CallEndController
from integrations.telephony.memory_outbox import (
    enqueue_phone_memory_in_transaction,
)
from integrations.telephony.message_audio import (
    PhoneMessageAudioRange,
    persist_message_audio_range_in_transaction,
)
from integrations.telephony.tooling import CallSchedulePolicy


_TURN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


@dataclass(slots=True)
class PhoneTurnLinkState:
    """Mutable playback outcome shared with the transaction callback."""

    interrupted: bool = False
    caller_audio_range: PhoneMessageAudioRange | None = None
    assistant_audio_range: PhoneMessageAudioRange | None = None

    def mark_interrupted(self) -> None:
        self.interrupted = True

    def set_audio_range(
        self,
        participant: Literal["caller", "assistant"],
        audio_range: PhoneMessageAudioRange,
    ) -> None:
        if participant not in {"caller", "assistant"}:
            raise ValueError("participant must be caller or assistant")
        if not isinstance(audio_range, PhoneMessageAudioRange):
            raise TypeError("audio_range must be a PhoneMessageAudioRange")
        attribute = f"{participant}_audio_range"
        existing = getattr(self, attribute)
        if existing is not None and existing != audio_range:
            raise RuntimeError("Phone turn audio range changed after capture")
        setattr(self, attribute, audio_range)


@dataclass(frozen=True, slots=True)
class PhoneChannelTurn:
    """Runtime context plus the playback state owned by one phone turn."""

    context: ChannelContext
    link_state: PhoneTurnLinkState
    end_controller: CallEndController


def create_phone_channel_turn(
    guard: ForegroundCommitGuard,
    *,
    turn_id: str,
    link_state: PhoneTurnLinkState | None = None,
    end_controller: CallEndController | None = None,
    internal_turn_context: str | None = None,
    openai_realtime_bridge: Any | None = None,
    ai_initiation_mode: (
        Literal["on_request", "proactive", "disabled"] | None
    ) = None,
    phone_direction: PhoneDirection | None = None,
    phone_request_source: PhoneRequestSource | None = None,
    phone_request_timing: PhoneRequestTiming | None = None,
    prompt_id: int | None = None,
    persistence: Literal["deferred", "ingest_only"] = "deferred",
    application_channel=None,
) -> PhoneChannelTurn:
    """Create one deferred, call-fenced phone context.

    ``turn_id`` is scoped by ``call_id`` and must be stable across a transport
    reconnect.  The returned mutable ``link_state`` is deliberately tiny: the
    media ledger marks it interrupted before confirming an audible prefix.
    """

    if guard.expected_owner != "phone" or not guard.call_id or not guard.lease_owner:
        raise ValueError("A phone foreground guard with call and lease owner is required")
    normalized_turn_id = str(turn_id or "").strip()
    if _TURN_ID_PATTERN.fullmatch(normalized_turn_id) is None:
        raise ValueError("turn_id is not a valid bounded identifier")
    if persistence not in {"deferred", "ingest_only"}:
        raise ValueError("phone turn persistence mode is invalid")
    if openai_realtime_bridge is not None and (
        persistence != "deferred"
        or getattr(
            openai_realtime_bridge,
            "_aurvek_internal_realtime_bridge",
            False,
        )
        is not True
    ):
        raise ValueError("phone realtime bridge is invalid")
    if ai_initiation_mode not in {None, "on_request", "proactive", "disabled"}:
        raise ValueError("phone AI initiation mode is invalid")
    if prompt_id is not None and int(prompt_id) <= 0:
        raise ValueError("phone prompt id must be positive")

    state = link_state or PhoneTurnLinkState()
    active_end_controller = end_controller or CallEndController()
    trusted_internal_context = str(internal_turn_context or "").strip()
    if len(trusted_internal_context) > 8_000:
        raise ValueError("internal_turn_context exceeds its limit")
    if any(
        ord(character) < 32 and character not in {"\n", "\t"}
        for character in trusted_internal_context
    ):
        raise ValueError("internal_turn_context contains unsupported controls")

    async def commit_guard(_context: ChannelContext, conn: Any) -> bool:
        if application_channel is not None:
            from integrations.applications.channels import get_application_channel_service
            await get_application_channel_service().revalidate(
                conn, application_channel, require_current_route=False,
                require_current_identity=True, capability='phone')
        return await assert_commit_guard_in_transaction(conn, guard)

    async def link_messages(commit: ChannelCommit, conn: Any) -> None:
        # Recheck immediately before the durable side effect.  The first guard
        # runs after BEGIN IMMEDIATE; this second check also catches a lease
        # deadline that elapsed while provider billing/messages were written.
        if not await assert_commit_guard_in_transaction(conn, guard):
            raise StaleChannelTurnError(
                f"Phone foreground changed before linking turn {normalized_turn_id}"
            )

        if commit.user_message_id is None:
            raise RuntimeError("A phone turn must persist its caller message")
        await _insert_compatible_link(
            conn,
            call_id=guard.call_id,
            message_id=int(commit.user_message_id),
            participant="caller",
            turn_id=normalized_turn_id,
            # With no audible assistant row, the caller link is the only
            # durable record of a generation interrupted before playback.
            interrupted=(
                state.interrupted and commit.assistant_message_id is None
            ),
            played_ms=None,
            confirmed_text=None,
            audio_range=state.caller_audio_range,
        )
        if persistence == "ingest_only" and not commit.persistence_only:
            await enqueue_phone_memory_in_transaction(
                conn,
                call_id=guard.call_id,
                message_id=int(commit.user_message_id),
            )
        if commit.assistant_message_id is not None:
            if (
                not commit.confirmed_text
                or commit.played_ms is None
                or commit.played_ms <= 0
            ):
                raise RuntimeError(
                    "A linked assistant message requires confirmed audible text"
                )
            await _insert_compatible_link(
                conn,
                call_id=guard.call_id,
                message_id=int(commit.assistant_message_id),
                participant="assistant",
                turn_id=normalized_turn_id,
                interrupted=state.interrupted,
                played_ms=commit.played_ms,
                confirmed_text=commit.confirmed_text,
                audio_range=state.assistant_audio_range,
            )
        elif commit.confirmed_text or (commit.played_ms not in {None, 0}):
            raise RuntimeError(
                "Audible assistant metadata requires an assistant message"
            )

    context = ChannelContext(
        channel="phone",
        application=application_channel.scope if application_channel else None,
        application_channel=application_channel,
        persistence=persistence,
        input_origin="phone.live_call",
        input_perception=(
            "audio_native"
            if openai_realtime_bridge is not None
            else "transcript_only"
        ),
        phone_direction=phone_direction,
        phone_request_source=phone_request_source,
        phone_request_timing=phone_request_timing,
        turn_key=TurnKey(call_id=guard.call_id, turn_id=normalized_turn_id),
        commit_guard=commit_guard,
        on_commit_in_transaction=link_messages,
        provenance={
            "phone_call_id": guard.call_id,
            "foreground_epoch": guard.epoch,
            "turn_id": normalized_turn_id,
            "end_call_controller": active_end_controller,
            "phone_memory_outbox": persistence == "ingest_only",
            **(
                {
                    "call_schedule_policy": CallSchedulePolicy(
                        ai_initiation_mode,
                        prompt_id=prompt_id,
                    )
                }
                if ai_initiation_mode in {"on_request", "proactive"}
                else {}
            ),
            **(
                {"openai_realtime_bridge": openai_realtime_bridge}
                if openai_realtime_bridge is not None
                else {}
            ),
            **(
                {"internal_turn_context": trusted_internal_context}
                if trusted_internal_context
                else {}
            ),
        },
    )
    return PhoneChannelTurn(
        context=context,
        link_state=state,
        end_controller=active_end_controller,
    )


async def _insert_compatible_link(
    conn: Any,
    *,
    call_id: str,
    message_id: int,
    participant: str,
    turn_id: str,
    interrupted: bool,
    played_ms: int | None,
    confirmed_text: str | None,
    audio_range: PhoneMessageAudioRange | None,
) -> None:
    """Insert exactly once, rejecting an incompatible pre-existing link."""

    await conn.execute(
        """
        INSERT INTO PHONE_CALL_MESSAGE_LINKS (
            call_id, message_id, participant, turn_id, origin_channel,
            interrupted, played_ms, confirmed_text, delivery_state
        ) VALUES (?, ?, ?, ?, 'phone', ?, ?, ?, 'consumed')
        ON CONFLICT(call_id, message_id) DO NOTHING
        """,
        (
            call_id,
            int(message_id),
            participant,
            turn_id,
            int(bool(interrupted)),
            played_ms,
            confirmed_text,
        ),
    )
    cursor = await conn.execute(
        """
        SELECT participant, turn_id, origin_channel, interrupted, played_ms,
               confirmed_text, delivery_state
        FROM PHONE_CALL_MESSAGE_LINKS
        WHERE call_id=? AND message_id=?
        """,
        (call_id, int(message_id)),
    )
    row = await cursor.fetchone()
    expected = (
        participant,
        turn_id,
        "phone",
        int(bool(interrupted)),
        played_ms,
        confirmed_text,
        "consumed",
    )
    if row is None or tuple(row) != expected:
        raise RuntimeError("Phone message is already linked incompatibly")
    if audio_range is not None:
        await persist_message_audio_range_in_transaction(
            conn,
            call_id=call_id,
            message_id=message_id,
            participant=participant,
            audio_range=audio_range,
        )


__all__ = [
    "PhoneChannelTurn",
    "PhoneMessageAudioRange",
    "PhoneTurnLinkState",
    "create_phone_channel_turn",
]
