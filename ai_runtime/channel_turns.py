"""Channel-neutral turn coordination and deferred persistence gates.

Voice transports may deliver generated text before they know how much audio was
actually heard.  This module keeps that transport concern outside providers and
the database layer: providers publish one draft through ``save_content_to_db``;
the active channel confirms an exact audible prefix; only then may the existing
canonical persistence transaction run.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Literal, Mapping, TypeVar


ChannelName = Literal["web", "whatsapp", "telegram", "device", "phone"]
PersistenceMode = Literal["immediate", "deferred", "ingest_only"]
InputOrigin = Literal[
    "web.message",
    "web.live_voice",
    "whatsapp.message",
    "whatsapp.voice_note",
    "whatsapp.audio",
    "telegram.message",
    "telegram.voice_note",
    "device.message",
    "phone.live_call",
]
InputPerception = Literal["text", "transcript_only", "audio_native"]
_DEFAULT_INPUT_ORIGIN: dict[ChannelName, InputOrigin] = {
    "web": "web.message",
    "whatsapp": "whatsapp.message",
    "telegram": "telegram.message",
    "device": "device.message",
    "phone": "phone.live_call",
}
_ALLOWED_INPUT_ORIGINS: dict[ChannelName, frozenset[InputOrigin]] = {
    "web": frozenset({"web.message", "web.live_voice"}),
    "whatsapp": frozenset(
        {"whatsapp.message", "whatsapp.voice_note", "whatsapp.audio"}
    ),
    "telegram": frozenset({"telegram.message", "telegram.voice_note"}),
    "device": frozenset({"device.message"}),
    "phone": frozenset({"phone.live_call"}),
}
_SPOKEN_INPUT_ORIGINS = frozenset(
    {
        "whatsapp.voice_note",
        "whatsapp.audio",
        "telegram.voice_note",
        "web.live_voice",
        "phone.live_call",
    }
)
_TEXT_INPUT_ORIGINS = frozenset(
    {"web.message", "whatsapp.message", "telegram.message", "device.message"}
)
CommitGuard = Callable[..., bool | Awaitable[bool]]
CommitObserver = Callable[["ChannelCommit"], None | Awaitable[None]]
TransactionalCommitObserver = Callable[
    ["ChannelCommit", Any], None | Awaitable[None]
]
StaleContextRecovery = Callable[
    ["ChannelContext"], "ChannelContext | None | Awaitable[ChannelContext | None]"
]


class ChannelTurnError(RuntimeError):
    """Base error for invalid or stale channel-turn operations."""


class DuplicateChannelTurnError(ChannelTurnError):
    """A different live owner already registered the same exact turn key."""


class StaleChannelTurnError(ChannelTurnError):
    """The durable commit guard rejected a stale foreground/fencing owner."""


class ChannelTurnCancelled(asyncio.CancelledError):
    """Cancellation scoped to one exact ``TurnKey``."""


class ChannelPersistenceError(ChannelTurnError):
    """The canonical channel turn could not be persisted."""


@dataclass(frozen=True, slots=True)
class TurnKey:
    call_id: str
    turn_id: str

    def __post_init__(self) -> None:
        if not str(self.call_id).strip() or not str(self.turn_id).strip():
            raise ValueError("TurnKey requires non-empty call_id and turn_id")


@dataclass(frozen=True, slots=True)
class ChannelContext:
    channel: ChannelName = "web"
    persistence: PersistenceMode = "immediate"
    turn_key: TurnKey | None = None
    commit_guard: CommitGuard | None = field(default=None, compare=False, repr=False)
    on_commit_in_transaction: TransactionalCommitObserver | None = field(
        default=None, compare=False, repr=False
    )
    on_commit: CommitObserver | None = field(default=None, compare=False, repr=False)
    recover_stale_context: StaleContextRecovery | None = field(
        default=None, compare=False, repr=False
    )
    provenance: Mapping[str, Any] = field(default_factory=dict, compare=False)
    input_origin: InputOrigin | None = None
    input_perception: InputPerception | None = None

    def __post_init__(self) -> None:
        if self.channel not in {"web", "whatsapp", "telegram", "device", "phone"}:
            raise ValueError(f"Unsupported channel: {self.channel}")
        if self.persistence not in {"immediate", "deferred", "ingest_only"}:
            raise ValueError(f"Unsupported persistence mode: {self.persistence}")
        if self.persistence == "deferred" and self.turn_key is None:
            raise ValueError("Deferred channel turns require a TurnKey")

        provenance = dict(self.provenance)
        input_origin = self.input_origin or _DEFAULT_INPUT_ORIGIN[self.channel]
        input_perception = self.input_perception or (
            "transcript_only" if self.channel == "phone" else "text"
        )
        if input_origin not in _ALLOWED_INPUT_ORIGINS[self.channel]:
            raise ValueError(
                f"Input origin {input_origin!r} is invalid for channel {self.channel!r}"
            )
        if input_perception not in {"text", "transcript_only", "audio_native"}:
            raise ValueError(f"Unsupported input perception: {input_perception}")
        if input_origin in _SPOKEN_INPUT_ORIGINS and input_perception == "text":
            raise ValueError(
                f"Spoken input origin {input_origin!r} cannot use text perception"
            )
        if input_origin in _TEXT_INPUT_ORIGINS and input_perception != "text":
            raise ValueError(
                f"Text input origin {input_origin!r} requires text perception"
            )
        if input_perception == "audio_native":
            bridge = provenance.get("openai_realtime_bridge")
            if (
                self.channel != "phone"
                or not getattr(bridge, "_aurvek_internal_realtime_bridge", False)
            ):
                raise ValueError(
                    "audio_native perception requires a trusted live phone audio bridge"
                )

        object.__setattr__(self, "input_origin", input_origin)
        object.__setattr__(self, "input_perception", input_perception)
        object.__setattr__(self, "provenance", MappingProxyType(provenance))

    @property
    def bypass_gransabio(self) -> bool:
        return self.channel == "phone" or self.persistence == "ingest_only"

    @property
    def ingest_only(self) -> bool:
        return self.persistence == "ingest_only"


@dataclass(frozen=True, slots=True)
class ChannelDraft:
    content: str


@dataclass(frozen=True, slots=True)
class AudibleConfirmation:
    text_prefix: str
    played_ms: int


@dataclass(frozen=True, slots=True)
class ChannelCommit:
    context: ChannelContext
    user_message_id: int | None
    assistant_message_id: int | None
    confirmed_text: str | None
    played_ms: int | None
    persistence_only: bool = False


T = TypeVar("T")


async def _maybe_await(value: T | Awaitable[T]) -> T:
    if inspect.isawaitable(value):
        return await value
    return value


async def assert_commit_guard_in_transaction(
    context: ChannelContext,
    conn: Any,
) -> None:
    """Assert fencing while the canonical write transaction owns its lock.

    New durable guards accept ``(context, conn)`` and perform their fencing
    SELECT/CAS on this exact connection after ``BEGIN IMMEDIATE``.  One-argument
    guards remain supported for callers that only need an in-memory epoch.
    """
    guard = context.commit_guard
    if guard is None:
        return
    try:
        parameters = inspect.signature(guard).parameters.values()
        accepts_connection = any(
            parameter.kind in {
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            }
            for parameter in parameters
        ) or len(inspect.signature(guard).parameters) >= 2
    except (TypeError, ValueError):
        accepts_connection = True
    allowed = await _maybe_await(
        guard(context, conn) if accepts_connection else guard(context)
    )
    if not allowed:
        raise StaleChannelTurnError(
            f"Commit guard rejected turn {context.turn_key!r}"
        )


class ChannelTurnHandle:
    """One live turn, including its draft, confirmation and one-shot commit."""

    def __init__(self, context: ChannelContext) -> None:
        if context.turn_key is None:
            raise ValueError("A registered channel turn requires a TurnKey")
        self.context = context
        self.key = context.turn_key
        self._draft_ready = asyncio.Event()
        self._draft: ChannelDraft | None = None
        self._confirmation: asyncio.Future[AudibleConfirmation] = (
            asyncio.get_running_loop().create_future()
        )
        self._commit_result: asyncio.Future[tuple[int | None, int | None]] = (
            asyncio.get_running_loop().create_future()
        )
        self._owner_task: asyncio.Task[Any] | None = None
        self._interruption_fallback: Callable[..., Awaitable[
            tuple[int | None, int | None]
        ]] | None = None
        self._fallback_claimed = False
        self._claim: Literal["open", "deferred", "interruption", "closed"] = "open"
        self._interrupt_requested = False
        self._terminal_error: BaseException | None = None
        self._persistence_task: asyncio.Task[tuple[int | None, int | None]] | None = None
        self._lock = asyncio.Lock()

    @property
    def draft(self) -> ChannelDraft | None:
        return self._draft

    @property
    def committed(self) -> bool:
        return (
            self._commit_result.done()
            and not self._commit_result.cancelled()
            and self._commit_result.exception() is None
        )

    def bind_owner_task(self, task: asyncio.Task[Any] | None = None) -> None:
        owner = task or asyncio.current_task()
        if owner is not None and self._owner_task not in {None, owner}:
            raise DuplicateChannelTurnError(f"Turn {self.key!r} already has an owner")
        self._owner_task = owner

    def register_interruption_fallback(
        self,
        commit: Callable[..., Awaitable[tuple[int | None, int | None]]],
    ) -> None:
        """Register user/prefix persistence before provider generation starts."""
        if self._interruption_fallback not in {None, commit}:
            raise ChannelTurnError("The interruption fallback is already registered")
        self._interruption_fallback = commit

    async def wait_for_draft(self) -> ChannelDraft:
        await self._draft_ready.wait()
        if self._draft is None:
            if self._terminal_error is not None:
                raise self._terminal_error
            raise ChannelTurnCancelled(f"Turn {self.key!r} was cancelled")
        return self._draft

    async def wait_for_commit(self) -> tuple[int | None, int | None]:
        """Wait for the one canonical durable result without exposing its future."""

        return await asyncio.shield(self._commit_result)

    @staticmethod
    def _require_persisted_result(
        result: tuple[int | None, int | None],
    ) -> tuple[int | None, int | None]:
        if not any(message_id is not None for message_id in result):
            raise ChannelPersistenceError("Channel turn persistence returned no message IDs")
        return result

    def database_commit_completed(
        self,
        result: tuple[int | None, int | None],
    ) -> None:
        """Signal the durable commit boundary and stop an interrupted owner.

        This callback is invoked immediately after the SQLite commit, while the
        independent persistence task may continue Atagia/watchdog work.
        """
        result = self._require_persisted_result(result)
        if self._interrupt_requested and not self._commit_result.done():
            self._commit_result.set_result(result)
        owner = self._owner_task
        if self._interrupt_requested and owner is not None and not owner.done():
            asyncio.get_running_loop().call_soon(owner.cancel, "barge_in_committed")

    def confirm_audible_prefix(self, text_prefix: str, *, played_ms: int) -> None:
        if self._draft is None:
            raise ChannelTurnError("Cannot confirm audio before a draft is published")
        prefix = str(text_prefix)
        duration = int(played_ms)
        if duration < 0:
            raise ValueError("played_ms cannot be negative")
        if duration == 0 and prefix:
            raise ValueError("A 0 ms confirmation cannot contain audible text")
        if duration > 0 and not prefix:
            raise ValueError("Audible playback requires a non-empty confirmed prefix")
        if not self._draft.content.startswith(prefix):
            raise ValueError("Confirmed text must be an exact prefix of the published draft")
        confirmation = AudibleConfirmation(text_prefix=prefix, played_ms=duration)
        if self._confirmation.done():
            previous = self._confirmation.result()
            if previous != confirmation:
                raise ChannelTurnError("The audible confirmation is already final")
            return
        self._confirmation.set_result(confirmation)

    @staticmethod
    def _unpublished_confirmation(
        text_prefix: str,
        *,
        played_ms: int,
    ) -> AudibleConfirmation:
        prefix = str(text_prefix)
        duration = int(played_ms)
        if duration < 0:
            raise ValueError("played_ms cannot be negative")
        if duration == 0 and prefix:
            raise ValueError("A 0 ms confirmation cannot contain audible text")
        if duration > 0 and not prefix:
            raise ValueError("Audible playback requires a non-empty confirmed prefix")
        return AudibleConfirmation(text_prefix=prefix, played_ms=duration)

    async def interrupt_and_commit(
        self,
        text_prefix: str,
        *,
        played_ms: int,
        reason: str = "barge_in",
    ) -> tuple[int | None, int | None]:
        """Commit heard output (or user-only at 0 ms), then cancel generation.

        Before a provider publishes its final draft, the transport ledger is the
        authority for ``text_prefix``.  Once a draft exists, the stricter exact
        prefix validation is used.  Both paths converge on the same one-shot
        result future, so a provider reaching its normal save concurrently
        cannot produce a second message or charge.
        """
        fallback_owner = False
        async with self._lock:
            if self._commit_result.done():
                result_future = self._commit_result
            elif self._claim == "closed":
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ChannelTurnCancelled(f"Turn {self.key!r} is closed")
            elif self._claim == "deferred":
                self._interrupt_requested = True
                self.confirm_audible_prefix(text_prefix, played_ms=played_ms)
                result_future = self._commit_result
            else:
                if self._interruption_fallback is None:
                    raise ChannelTurnError(
                        "Interruption fallback must be registered before generation"
                    )
                if not self._fallback_claimed:
                    self._fallback_claimed = True
                    self._claim = "interruption"
                    self._interrupt_requested = True
                    fallback_owner = True
                result_future = self._commit_result

        if fallback_owner:
            confirmation = self._unpublished_confirmation(
                text_prefix, played_ms=played_ms
            )
            try:
                fallback = self._interruption_fallback
                try:
                    parameter_count = len(inspect.signature(fallback).parameters)
                except (TypeError, ValueError):
                    parameter_count = 2
                if parameter_count >= 2:
                    result = await fallback(
                        confirmation, self.database_commit_completed
                    )
                else:
                    result = await fallback(confirmation)
                    self.database_commit_completed(result)
                result = self._require_persisted_result(result)
                if any(message_id is not None for message_id in result):
                    await self.notify_commit(
                        result,
                        confirmed_text=confirmation.text_prefix or None,
                        played_ms=confirmation.played_ms,
                    )
                if not self._commit_result.done():
                    self._commit_result.set_result(result)
            except BaseException as exc:
                if not self._commit_result.done():
                    if isinstance(exc, asyncio.CancelledError):
                        self._commit_result.cancel()
                    else:
                        self._commit_result.set_exception(exc)
                        self._commit_result.exception()
                owner = self._owner_task
                if (owner is not None and owner is not asyncio.current_task()
                        and not owner.done()):
                    owner.cancel("interruption_persistence_failed")
                raise

        result = await asyncio.shield(result_future)
        owner = self._owner_task
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            owner.cancel(reason)
        return result

    def cancel(self, reason: str = "channel_turn_cancelled") -> None:
        self._claim = "closed"
        if not self._confirmation.done():
            self._confirmation.cancel(reason)
        if not self._commit_result.done():
            self._commit_result.cancel(reason)
        self._draft_ready.set()
        owner = self._owner_task
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            owner.cancel(reason)

    async def close_unfinished(self, reason: str) -> bool:
        """Close a provider stream that ended without publishing or committing."""
        async with self._lock:
            if self._commit_result.done() or self._claim == "closed":
                return False
            if self._claim in {"deferred", "interruption"}:
                # A durable commit task owns terminalization now.
                return False
            self._claim = "closed"
            error = ChannelPersistenceError(reason)
            self._terminal_error = error
            if not self._confirmation.done():
                self._confirmation.set_exception(error)
                self._confirmation.exception()
            if not self._commit_result.done():
                self._commit_result.set_exception(error)
                self._commit_result.exception()
            self._draft_ready.set()
            return True

    async def defer_commit(
        self,
        draft_content: str,
        commit: Callable[[AudibleConfirmation], Awaitable[tuple[int | None, int | None]]],
    ) -> tuple[int | None, int | None]:
        """Publish once, await exact playback confirmation and commit exactly once."""
        owner = False
        wait_for_existing = False
        async with self._lock:
            draft = ChannelDraft(content=str(draft_content or ""))
            if self._claim == "interruption":
                wait_for_existing = True
            elif self._claim == "closed":
                if self._terminal_error is not None:
                    raise self._terminal_error
                raise ChannelTurnCancelled(f"Turn {self.key!r} is closed")
            elif self._claim == "open":
                self._claim = "deferred"
                self._draft = draft
                self._draft_ready.set()
                owner = True
            elif self._draft != draft:
                raise ChannelTurnError("A turn cannot publish two different final drafts")

        if wait_for_existing or not owner:
            return await asyncio.shield(self._commit_result)

        try:
            confirmation = await self._confirmation
            self._persistence_task = asyncio.create_task(commit(confirmation))
            result = await asyncio.shield(self._persistence_task)
            result = self._require_persisted_result(result)
            if not self._commit_result.done():
                self._commit_result.set_result(result)
            return result
        except BaseException as exc:
            if not self._commit_result.done():
                if isinstance(exc, asyncio.CancelledError):
                    self._commit_result.cancel()
                else:
                    self._commit_result.set_exception(exc)
                    # The owner receives ``exc`` directly; mark the shared
                    # future observed while retaining it for idempotent waiters.
                    self._commit_result.exception()
            raise

    async def notify_commit(
        self,
        result: tuple[int | None, int | None],
        *,
        confirmed_text: str | None,
        played_ms: int | None,
    ) -> None:
        observer = self.context.on_commit
        if observer is None:
            return
        try:
            await _maybe_await(
                observer(
                    ChannelCommit(
                        context=self.context,
                        user_message_id=result[0],
                        assistant_message_id=result[1],
                        confirmed_text=confirmed_text,
                        played_ms=played_ms,
                    )
                )
            )
        except Exception:
            logging.getLogger(__name__).warning(
                "Post-commit channel observer failed for %r",
                self.key,
                exc_info=True,
            )


class ChannelTurnRegistry:
    """In-memory live-owner registry keyed by the full call + turn identity."""

    def __init__(self) -> None:
        self._handles: dict[TurnKey, ChannelTurnHandle] = {}
        self._lock = asyncio.Lock()

    async def register(self, context: ChannelContext) -> ChannelTurnHandle:
        if context.turn_key is None:
            raise ValueError("Registered channel contexts require a TurnKey")
        async with self._lock:
            if context.turn_key in self._handles:
                raise DuplicateChannelTurnError(
                    f"Turn {context.turn_key!r} is already registered"
                )
            handle = ChannelTurnHandle(context)
            self._handles[context.turn_key] = handle
            return handle

    async def get(self, key: TurnKey) -> ChannelTurnHandle | None:
        async with self._lock:
            return self._handles.get(key)

    async def unregister(self, key: TurnKey, handle: ChannelTurnHandle) -> bool:
        async with self._lock:
            if self._handles.get(key) is not handle:
                return False
            del self._handles[key]
            return True

    async def cancel(self, key: TurnKey, reason: str = "channel_turn_cancelled") -> bool:
        async with self._lock:
            handle = self._handles.get(key)
        if handle is None:
            return False
        handle.cancel(reason)
        return True


@dataclass(frozen=True, slots=True)
class BoundChannelTurn:
    context: ChannelContext
    handle: ChannelTurnHandle | None = None


_active_channel_turn: ContextVar[BoundChannelTurn | None] = ContextVar(
    "active_channel_turn", default=None
)


def current_channel_turn() -> BoundChannelTurn | None:
    return _active_channel_turn.get()


@contextmanager
def bind_channel_turn(
    context: ChannelContext,
    handle: ChannelTurnHandle | None = None,
):
    if context.persistence == "deferred" and handle is None:
        raise ValueError("Deferred contexts must bind their registered turn handle")
    token = _active_channel_turn.set(BoundChannelTurn(context=context, handle=handle))
    try:
        yield
    finally:
        _active_channel_turn.reset(token)


channel_turn_registry = ChannelTurnRegistry()
